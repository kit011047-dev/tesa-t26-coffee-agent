"""[T26] pi_view.py: อ่านค่าจาก Raspberry Pi มาแสดงบน dashboard อย่างเดียว (ไม่ใช้ตั้งราคา)

    Track 1  ESP32 วัดความชื้น  ──USB──→  Pi: pi_server :8100  ←── Track 2 YOLO นับ/แยกเมล็ด (outbox/<lot_id>.json)
                                               │ HTTP (อ่านอย่างเดียว)
                                               ↓
                               Track 3 agent (ไฟล์นี้) → dashboard แสดงเทียบกับตัวอย่างจากตลาด

หลักความปลอดภัย:
  - ค่าจาก Pi ไม่ถูกส่งเข้า appraisal.py / strategy.py เลย ราคาและ TV มาจากตัวอย่างของตลาดเท่านั้น
    (ยังไม่ได้รับการยืนยันจากทีมงานว่ากติกาให้ใช้ข้อมูลเซนเซอร์ตั้งราคา)
  - อ่านในเธรดเบื้องหลัง agent ไม่ต้องรอ Pi · Pi ล่ม/ช้า = แค่แสดงว่าออฟไลน์ ไม่กระทบการขาย
  - ปิดเป็นค่าเริ่มต้น เปิดด้วย PI_ENABLED=1 ใน .env (PI_URL=http://<IP Pi>:8100)
  - PI_SOURCE=http (ค่าเริ่มต้น: ถาม pi_server ทุก 10 วิ) | mqtt (subscribe t26/lot/# ที่ broker บน Pi :1883)

ลองเอง:  python pi_view.py            # อ่าน Pi ครั้งเดียวแล้วพิมพ์ผล
"""
import json
import os
import threading
import time

import httpx

GOOD = ("good", "normal")
_lock = threading.Lock()
_snap: dict = {"online": False, "checked": None, "error": "ยังไม่ได้อ่าน", "mode": None, "lots": {}}
_market_lots: list[str] = []
_thread: threading.Thread | None = None


def enabled() -> bool:
    return os.environ.get("PI_ENABLED", "0") == "1"


def url() -> str:
    return os.environ.get("PI_URL", "http://172.16.11.205:8100").rstrip("/")


def defect_pct(counts: dict) -> float | None:
    total = sum(v for v in counts.values() if isinstance(v, (int, float)))
    if total <= 0:
        return None
    good = sum(counts.get(k, 0) for k in GOOD)
    return round(100.0 * (total - good) / total, 1)


def read_once(lot_ids: list[str], timeout: float = 2.0) -> dict:
    """อ่าน Pi 1 รอบ: /health แล้ว /measure ของทุกล็อต (ล็อตตลาด + ล็อตที่ Track 2 มีผล)"""
    snap = {"online": False, "checked": time.time(), "error": None, "mode": None, "lots": {}}
    try:
        with httpx.Client(timeout=timeout) as c:
            h = c.get(url() + "/health").json()
            snap["online"], snap["mode"] = bool(h.get("ok")), h.get("mode")
            t2 = h.get("track2") or {}
            snap["track2_lots"] = list(t2.get("lots") or [])
            ids = list(dict.fromkeys(list(lot_ids) + snap["track2_lots"]))
            for lid in ids:
                r = c.get(url() + "/measure", params={"lot_id": lid})
                body = r.json()
                if r.status_code != 200:
                    snap["lots"][lid] = {"missing": (body.get("detail") or {}).get("missing", {})}
                    continue
                row = {"missing": body.get("missing", {})}
                if body.get("bean_counts"):
                    row["bean_counts"] = body["bean_counts"]
                    row["beans"] = sum(body["bean_counts"].values())
                    row["defect_pct"] = defect_pct(body["bean_counts"])
                for k in ("moisture_pct", "weight_kg"):
                    if body.get(k) is not None:
                        row[k] = body[k]
                row["confidence"] = (body.get("track2") or {}).get("avg_confidence")
                row["t1_device"] = (body.get("track1") or {}).get("device_id")
                snap["lots"][lid] = row
    except Exception as e:                                  # noqa: BLE001 — Pi ล่มต้องไม่ทำให้ agent ล่ม
        snap["online"], snap["error"] = False, f"{type(e).__name__}: {e}"[:160]
    return snap


# ---------------------------------------------------------------------
# [T26] ทางเลือก MQTT (PI_SOURCE=mqtt): T1/T2 publish เข้า broker บน Pi แล้ว agent subscribe
#   t26/lot/<lot_id>/moisture  <- T1 (JSON เหมือนไฟล์ track1/<lot>.json)
#   t26/lot/<lot_id>/quality   <- T2 (JSON เหมือนไฟล์ outbox/<lot>.json)
# ไม่มี paho-mqtt หรือต่อ broker ไม่ได้ = แค่แสดงว่าออฟไลน์ agent เล่นต่อได้
_mq_data: dict = {}            # lot_id -> {"moisture": {...}, "quality": {...}, "at": เวลาที่ได้รับล่าสุด}
_mq_state = {"connected": False, "error": "ยังไม่ได้ต่อ broker"}


def source() -> str:
    return os.environ.get("PI_SOURCE", "http").lower()


def mqtt_host() -> tuple[str, int]:
    from urllib.parse import urlparse
    host = os.environ.get("PI_MQTT_HOST") or urlparse(url()).hostname or "127.0.0.1"
    return host, int(os.environ.get("PI_MQTT_PORT", "1883"))


def _mq_on_message(client, userdata, msg) -> None:
    try:
        parts = msg.topic.split("/")                       # t26/lot/<lot_id>/<kind>
        if len(parts) != 4 or parts[3] not in ("moisture", "quality"):
            return
        doc = json.loads(msg.payload.decode("utf-8"))
        with _lock:
            d = _mq_data.setdefault(parts[2], {})
            d[parts[3]], d["at"] = doc, time.time()
    except Exception as e:                                  # noqa: BLE001 — ข้อความเพี้ยนต้องไม่ทำให้ agent ล่ม
        _mq_state["error"] = f"ข้อความเพี้ยน {msg.topic}: {e}"[:160]


def mqtt_snapshot() -> dict:
    """แปลงข้อความ MQTT ที่ได้รับเป็นรูปแบบเดียวกับ read_once()"""
    snap = {"online": _mq_state["connected"], "checked": time.time(), "mode": "mqtt",
            "error": None if _mq_state["connected"] else _mq_state["error"], "lots": {}}
    with _lock:
        items = {k: dict(v) for k, v in _mq_data.items()}
    for lid, d in items.items():
        row: dict = {"missing": {}}
        m, q = d.get("moisture"), d.get("quality")
        if m and m.get("moisture_pct") is not None:
            row["moisture_pct"], row["t1_device"] = m["moisture_pct"], m.get("device_id")
        else:
            row["missing"]["moisture_pct"] = "ยังไม่มีข้อความจาก T1"
        if q and q.get("counts"):
            row["bean_counts"] = {k: int(v) for k, v in q["counts"].items()}
            row["beans"] = sum(row["bean_counts"].values())
            row["defect_pct"] = defect_pct(row["bean_counts"])
            row["confidence"] = q.get("avg_confidence")
        else:
            row["missing"]["bean_counts"] = "ยังไม่มีข้อความจาก T2"
        snap["lots"][lid] = row
    return snap


def _mqtt_start() -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        _mq_state["error"] = "ไม่มี paho-mqtt (pip install paho-mqtt)"
        return

    def on_connect(client, userdata, flags, reason, props=None):
        ok = not reason.is_failure
        _mq_state["connected"], _mq_state["error"] = bool(ok), None if ok else f"broker ปฏิเสธ: {reason}"
        if ok:
            client.subscribe("t26/lot/#", qos=1)

    def on_disconnect(client, userdata, flags, reason, props=None):
        _mq_state["connected"], _mq_state["error"] = False, f"หลุดจาก broker: {reason}"

    host, port = mqtt_host()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"t26-agent-{os.getpid()}")
    c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, _mq_on_message
    c.reconnect_delay_set(1, 10)
    try:
        c.connect_async(host, port, keepalive=30)          # ไม่รอ: broker ไม่อยู่ก็ไม่ค้าง
        c.loop_start()                                     # เธรดของ paho ต่อใหม่เองเมื่อหลุด
    except Exception as e:                                  # noqa: BLE001
        _mq_state["error"] = f"{type(e).__name__}: {e}"[:160]


def _loop(every: float) -> None:
    global _snap
    while True:
        with _lock:
            ids = list(_market_lots)
        s = mqtt_snapshot() if source() == "mqtt" else read_once(ids)
        with _lock:
            _snap = s
        time.sleep(every)


def start() -> None:
    """เริ่มเธรดอ่าน Pi (ครั้งเดียว) ถ้า PI_ENABLED=1"""
    global _thread
    if not enabled() or _thread is not None:
        return
    if source() == "mqtt":
        _mqtt_start()
    every = float(os.environ.get("PI_POLL_S", "10" if source() == "http" else "2"))
    _thread = threading.Thread(target=_loop, args=(every,), daemon=True, name="pi_view")
    _thread.start()


def set_market_lots(lot_ids: list[str]) -> None:
    with _lock:
        _market_lots[:] = list(lot_ids)


def snapshot() -> dict:
    with _lock:
        return dict(_snap)


def compare(state: dict, snap: dict) -> dict:
    """เทียบรายล็อต: ตัวอย่างจากตลาด vs ค่าจาก Pi (เพื่อแสดงผล)"""
    rows, seen = [], set()
    for l in state.get("lots", []):
        lid, s = l["lot_id"], l.get("sample", {})
        p = snap.get("lots", {}).get(lid, {})
        seen.add(lid)
        rows.append({"lot_id": lid, "in_market": True,
                     "market_defect": s.get("defect_pct"), "market_moisture": s.get("moisture_pct"),
                     "pi_defect": p.get("defect_pct"), "pi_moisture": p.get("moisture_pct"),
                     "beans": p.get("beans"), "bean_counts": p.get("bean_counts"), "missing": p.get("missing"),
                     "confidence": p.get("confidence"), "t1_device": p.get("t1_device")})
    for lid, p in snap.get("lots", {}).items():
        if lid not in seen:
            rows.append({"lot_id": lid, "in_market": False, "market_defect": None, "market_moisture": None,
                         "pi_defect": p.get("defect_pct"), "pi_moisture": p.get("moisture_pct"),
                         "beans": p.get("beans"), "bean_counts": p.get("bean_counts"), "missing": p.get("missing"),
                     "confidence": p.get("confidence"), "t1_device": p.get("t1_device")})
    age = round(time.time() - snap["checked"]) if snap.get("checked") else None
    return {"enabled": True, "url": url() if source() == "http" else "mqtt://%s:%d" % mqtt_host(), "online": snap.get("online"), "mode": snap.get("mode"),
            "error": snap.get("error"), "age_s": age, "used_for_pricing": False, "lots": rows}


if __name__ == "__main__":
    import json
    from pathlib import Path
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if source() == "mqtt":
        _mqtt_start()
        time.sleep(float(os.environ.get("PI_WAIT_S", "3")))
        s = mqtt_snapshot()
    else:
        s = read_once([])
    print(json.dumps(compare({"lots": []}, s), ensure_ascii=False, indent=1))
