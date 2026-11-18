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

ลองเอง:  python pi_view.py            # อ่าน Pi ครั้งเดียวแล้วพิมพ์ผล
"""
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
                snap["lots"][lid] = row
    except Exception as e:                                  # noqa: BLE001 — Pi ล่มต้องไม่ทำให้ agent ล่ม
        snap["online"], snap["error"] = False, f"{type(e).__name__}: {e}"[:160]
    return snap


def _loop(every: float) -> None:
    global _snap
    while True:
        with _lock:
            ids = list(_market_lots)
        s = read_once(ids)
        with _lock:
            _snap = s
        time.sleep(every)


def start() -> None:
    """เริ่มเธรดอ่าน Pi (ครั้งเดียว) ถ้า PI_ENABLED=1"""
    global _thread
    if not enabled() or _thread is not None:
        return
    every = float(os.environ.get("PI_POLL_S", "10"))
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
                     "beans": p.get("beans"), "bean_counts": p.get("bean_counts"), "missing": p.get("missing")})
    for lid, p in snap.get("lots", {}).items():
        if lid not in seen:
            rows.append({"lot_id": lid, "in_market": False, "market_defect": None, "market_moisture": None,
                         "pi_defect": p.get("defect_pct"), "pi_moisture": p.get("moisture_pct"),
                         "beans": p.get("beans"), "bean_counts": p.get("bean_counts"), "missing": p.get("missing")})
    age = round(time.time() - snap["checked"]) if snap.get("checked") else None
    return {"enabled": True, "url": url(), "online": snap.get("online"), "mode": snap.get("mode"),
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
    s = read_once([])
    print(json.dumps(compare({"lots": []}, s), ensure_ascii=False, indent=1))
