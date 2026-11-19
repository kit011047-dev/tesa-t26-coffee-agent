"""
pi_server.py: รันบน Raspberry Pi เปิด API ให้ agent (บน Mac) มาดึงค่าเซนเซอร์

โหมด (ตัวแปร SENSOR_MODE):
    fake   สุ่มค่าทั้งหมด (ซ้อมได้โดยไม่มีฮาร์ดแวร์)
    esp32  ความชื้น + น้ำหนัก จาก ESP32 ทาง USB serial (เธรดเบื้องหลังอ่านตลอด)
           จำนวนเมล็ด จากไฟล์ผลของ Track 2: {TRACK2_OUTBOX}/{lot_id}.json
    yolo   จำนวนเมล็ดจาก Track 2 อย่างเดียว (ยังไม่มีเซนเซอร์ ความชื้น/น้ำหนักจะบอกว่า missing
           ไม่ส่งค่าสุ่มไปหลอก agent)

รัน:
    SENSOR_MODE=esp32 python -m uvicorn pi_server:app --host 0.0.0.0 --port 8100

Endpoint:
    GET /health              สถานะ + ค่าล่าสุดจาก ESP32
    GET /measure?lot_id=L01  วัดล็อตนี้ (field ไหนอ่านไม่ได้จะไม่ใส่ และบอกใน "missing")
    GET /esp32               บรรทัดล่าสุดจาก ESP32 (ไว้ debug)
    GET /lots                ผลวัดที่จำไว้ทั้งหมด
    POST /track1             [T26] รับความชื้นจาก Mac ที่ต่อ ESP32: {lot_id, moisture_pct, device_id?, n_readings?} → เขียน {TRACK1_DIR}/{lot_id}.json
"""
import glob
import json
import os
import random
import threading
import time
from pathlib import Path

import re
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException

MODE = os.environ.get("SENSOR_MODE", "fake")
SERIAL_PORT = os.environ.get("SERIAL_PORT", "")          # ว่าง = หาเอง (/dev/serial/by-id/* → /dev/ttyACM0)
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
MAX_READING_AGE_S = float(os.environ.get("MAX_READING_AGE_S", "5"))   # ค่าจาก ESP32 เก่ากว่านี้ = ใช้ไม่ได้
ALLOW_SIM = os.environ.get("ALLOW_SIM", "1") == "1"      # ยอมรับค่าที่ ESP32 จำลอง (sim=1) ไหม
TRACK2_OUTBOX = Path(os.path.expanduser(os.environ.get("TRACK2_OUTBOX", "~/tesa_work/outbox")))
TRACK1_DIR = Path(os.path.expanduser(os.environ.get("TRACK1_DIR", "~/tesa_work/track1")))   # [19 พ.ย.] ไฟล์ความชื้นของ Track 1 ต่อล็อต

CLASSES = ["good", "shell", "full_black", "hull_husk", "broken", "parchment", "immature"]
app = FastAPI(title="Coffee Sensor Pi")
_measured: dict[str, dict] = {}
_rng = random.Random(int(os.environ.get("FAKE_SEED", "7")))


# =====================================================================
# ESP32: เธรดอ่าน serial ตลอดเวลา เก็บบรรทัด JSON ล่าสุด
# =====================================================================
class ESP32Reader:
    def __init__(self):
        self.latest: dict | None = None
        self.latest_at = 0.0
        self.port_name = ""
        self.error = ""
        self._ser = None
        self._lock = threading.Lock()

    def _find_port(self) -> str:
        if SERIAL_PORT:
            return SERIAL_PORT
        found = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        return found[0] if found else ""

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import serial                                   # pyserial
        while True:
            try:
                self.port_name = self._find_port()
                if not self.port_name:
                    raise OSError("ไม่เจอพอร์ต ESP32 (เสียบสายหรือยัง?)")
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = self.port_name, SERIAL_BAUD, 1
                ser.dtr = False; ser.rts = False        # ไม่ reset ESP32 ตอนเปิดพอร์ต
                ser.open()
                self._ser, self.error = ser, ""
                while True:
                    line = ser.readline().decode("utf-8", "replace").strip()
                    if not line.startswith("{"):
                        continue                        # ข้อความบูตของ ESP32 ฯลฯ
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if "moisture_pct" in obj or "weight_kg" in obj:
                        with self._lock:
                            self.latest, self.latest_at = obj, time.time()
            except Exception as e:                      # สายหลุด/พอร์ตหาย → รอแล้วลองใหม่ ไม่ให้ server ล่ม
                self.error = f"{type(e).__name__}: {e}"
                self._ser = None
                time.sleep(2)

    def send(self, cmd: str):
        if self._ser:
            self._ser.write((cmd + "\n").encode())

    def reading(self) -> dict:
        """ค่าล่าสุดที่ยังสดและเชื่อถือได้ ไม่งั้น raise"""
        with self._lock:
            obj, at = self.latest, self.latest_at
        if obj is None:
            raise RuntimeError(f"ยังไม่ได้ค่าจาก ESP32 ({self.error or 'รอข้อมูล'})")
        if time.time() - at > MAX_READING_AGE_S:
            raise RuntimeError(f"ค่าจาก ESP32 เก่า {time.time() - at:.0f} วินาที ({self.error or 'ESP32 หยุดส่ง?'})")
        if obj.get("sim") and not ALLOW_SIM:
            raise RuntimeError("ESP32 ยังส่งค่าจำลอง (SIMULATE=1)")
        return obj


esp = ESP32Reader()
if MODE == "esp32":
    esp.start()


# =====================================================================
# อ่านค่าแต่ละอย่าง
# =====================================================================
def _no_sensor():
    raise RuntimeError("โหมด yolo: ยังไม่ได้ต่อเซนเซอร์")


def read_track1(lot_id: str) -> dict | None:
    """[19 พ.ย.] ข้อตกลงร่วม (INTERFACE.md): Track 1 เขียน ~/tesa_work/track1/<lot_id>.json มี moisture_pct"""
    f = TRACK1_DIR / f"{lot_id}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    m = float(d["moisture_pct"])
    if not 0 < m < 100:
        raise ValueError(f"Track 1 moisture_pct ผิดช่วง: {m}")
    return d


def read_moisture(lot_id: str = "") -> float:
    if MODE != "fake" and lot_id:
        d = read_track1(lot_id)                              # ไฟล์ของ Track 1 มาก่อน (ใช้ได้ทุกโหมดยกเว้น fake)
        if d is not None:
            return round(float(d["moisture_pct"]), 2)
    if MODE == "fake":
        return round(_rng.uniform(10.5, 13.5), 1)
    if MODE == "yolo":
        _no_sensor()
    return round(float(esp.reading()["moisture_pct"]), 2)


def read_weight_kg() -> float:
    if MODE == "fake":
        return float(_rng.randrange(300, 1501, 50))
    if MODE == "yolo":
        _no_sensor()
    return round(float(esp.reading()["weight_kg"]), 2)


def read_bean_counts(lot_id: str) -> dict:
    if MODE == "fake":
        counts = {c: _rng.randint(0, 12) for c in CLASSES[1:]}
        counts["good"] = 300 - sum(counts.values())
        return counts
    # ผลจากระบบนับเมล็ดของ Track 2 (ไฟล์เขียนแบบ atomic จึงไม่เจอไฟล์ครึ่ง ๆ)
    f = TRACK2_OUTBOX / f"{lot_id}.json"
    if not f.exists():
        err = TRACK2_OUTBOX / f"{lot_id}.error.txt"            # Track 2 เขียนไฟล์นี้เมื่อล็อตนั้นพัง
        if err.exists():
            raise RuntimeError(f"Track 2 นับล็อตนี้ไม่สำเร็จ: {err.read_text(encoding='utf-8')[:120]}")
        raise FileNotFoundError(f"ยังไม่มีผลนับเมล็ด {f}")
    doc = json.loads(f.read_text(encoding="utf-8"))
    counts = doc["counts"]
    counts = {c: int(counts.get(c, 0)) for c in CLASSES}
    if sum(counts.values()) == 0:
        raise ValueError("Track 2 นับได้ 0 เมล็ด")
    return counts


def track_meta(lot_id: str) -> dict:
    """[19 พ.ย.] ข้อมูลประกอบ: เครื่องวัดของ T1 และความมั่นใจของโมเดล T2 (ไม่มีก็ข้าม)"""
    meta = {}
    try:
        d = read_track1(lot_id)
        if d:
            meta["track1"] = {k: d.get(k) for k in ("device_id", "n_readings", "timestamp")}
    except Exception:                                        # noqa: BLE001
        pass
    try:
        f = TRACK2_OUTBOX / f"{lot_id}.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            meta["track2"] = {k: d.get(k) for k in ("avg_confidence", "images", "model", "timestamp")}
    except Exception:                                        # noqa: BLE001
        pass
    return meta


def outbox_info() -> dict:
    """สรุปสถานะโฟลเดอร์ผลของ Track 2 (ไว้เช็คจาก /health)"""
    if not TRACK2_OUTBOX.exists():
        return {"path": str(TRACK2_OUTBOX), "exists": False, "lots": []}
    lots = sorted(p.stem for p in TRACK2_OUTBOX.glob("*.json") if p.stem != "latest")
    return {"path": str(TRACK2_OUTBOX), "exists": True, "lots": lots}


# =====================================================================
# API
# =====================================================================
@app.get("/health")
def health():
    info = {"ok": True, "mode": MODE}
    if MODE in ("esp32", "yolo"):
        info["track2"] = outbox_info()
        info["track1"] = {"path": str(TRACK1_DIR), "exists": TRACK1_DIR.exists(),
                          "lots": sorted(p.stem for p in TRACK1_DIR.glob("*.json")) if TRACK1_DIR.exists() else []}
    if MODE == "esp32":
        age = time.time() - esp.latest_at if esp.latest_at else None
        info["esp32"] = {"port": esp.port_name, "error": esp.error, "latest": esp.latest,
                         "age_s": round(age, 1) if age is not None else None}
    return info


@app.get("/esp32")
def esp32_latest():
    return {"port": esp.port_name, "error": esp.error, "latest": esp.latest}


@app.get("/measure")
def measure(lot_id: str):
    result = {"lot_id": lot_id, "measured_at": time.time(), "mode": MODE, "missing": {}}
    for field, fn in (("moisture_pct", lambda: read_moisture(lot_id)), ("weight_kg", read_weight_kg),
                      ("bean_counts", lambda: read_bean_counts(lot_id))):
        try:
            result[field] = fn()
        except Exception as e:                       # field นี้อ่านไม่ได้ → ข้าม field เดียว ไม่ใช่ทั้งล็อต
            result["missing"][field] = str(e)
    if not any(k in result for k in ("moisture_pct", "weight_kg", "bean_counts")):
        raise HTTPException(503, {"error": "อ่านค่าไม่ได้สักอย่าง", "missing": result["missing"]})
    result.update(track_meta(lot_id))
    _measured[lot_id] = result
    return result


@app.get("/lots")
def lots():
    return _measured


@app.post("/track1")
def post_track1(body: dict = Body(...)):
    """[T26] Mac ที่ต่อ ESP32 ส่งความชื้นเข้ามา → เขียนเป็นไฟล์ track1/<lot_id>.json (read_track1 อ่านต่อ) · ทับของเดิมของล็อตนั้น"""
    lot_id = str(body.get("lot_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", lot_id):
        raise HTTPException(422, "lot_id ว่างหรือมีอักขระไม่อนุญาต (ใช้ A-Z a-z 0-9 . _ - ไม่เกิน 64)")
    try:
        m = float(body["moisture_pct"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "ต้องมี moisture_pct เป็นตัวเลข")
    if not 0 < m < 100:
        raise HTTPException(422, f"moisture_pct ผิดช่วง: {m}")
    n = body.get("n_readings")
    d = {"lot_id": lot_id, "moisture_pct": round(m, 2), "device_id": str(body.get("device_id") or "esp32")[:40],
         "n_readings": int(n) if isinstance(n, (int, float)) else None,
         "timestamp": body.get("timestamp") or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
    TRACK1_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TRACK1_DIR / f".{lot_id}.tmp"
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(TRACK1_DIR / f"{lot_id}.json")               # เขียนครั้งเดียวจบ ไม่ให้ GET /measure อ่านไฟล์ครึ่ง ๆ
    return {"ok": True, "saved": d}
