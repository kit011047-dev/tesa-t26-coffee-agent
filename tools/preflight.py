"""[T26] preflight.py: ตรวจความพร้อมก่อนรัน agent (run.sh เรียกให้เอง)

ตรวจ:
  1. .env มี MARKET_URL และ TEAM_KEY
  2. ต่อตลาดได้ (GET /v1/state อ่านอย่างเดียว ไม่ยื่นอะไร) -> บอกตลาด/ทีม/ช่วงเวลา/ล็อต
  3. log เก่าของตลาดซ้อมปนอยู่ไหม -> ย้ายออกไป ~/tesa-logs-archive/ ก่อนเกมใหม่เริ่ม
     (กรรมการเทียบ logs/decisions.jsonl กับที่ตลาดได้รับ จึงไม่ควรมี log ซ้อมปน)
     ย้ายเฉพาะตอนที่เรายังไม่ส่ง appraisal (= เกมยังไม่เริ่มสำหรับเรา)
     ถ้าส่งแล้ว = รีสตาร์ทกลางเกม -> ห้ามย้าย เพราะ agent ต้องใช้ความจำเดิมต่อ

exit code: 0 = พร้อม, 1 = ห้ามรัน (ไม่มี key / ต่อตลาดไม่ได้)
    python tools/preflight.py            # ตรวจปกติ
    python tools/preflight.py --clean    # บังคับย้าย log เก่า (เฉพาะตอนยังไม่ส่ง appraisal)
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
LOGS = HERE / "logs"
ARCHIVE = Path.home() / "tesa-logs-archive"


def load_env() -> dict:
    env = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def last_market_in_logs() -> str | None:
    f = LOGS / "decisions.jsonl"
    if not f.exists():
        return None
    market = None
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "new_game":
            market = e.get("market")
    return market


def archive_logs(reason: str) -> None:
    if not LOGS.exists() or not any(LOGS.iterdir()):
        return
    dest = ARCHIVE / time.strftime("%Y%m%d_%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for p in LOGS.iterdir():
        shutil.move(str(p), dest / p.name)
    print(f"  📦 ย้าย log เก่าไป {dest}  ({reason})")


def main() -> int:
    print("=== ตรวจความพร้อม ===")
    env = load_env()
    url, key = env.get("MARKET_URL", ""), env.get("TEAM_KEY", "")
    if not url or not key or "GATEWAY" in url:
        print("  ❌ .env ยังไม่มี MARKET_URL หรือ TEAM_KEY -> เปิด ~/tesa-starter/.env แล้วใส่ค่า")
        return 1
    print(f"  ✅ .env: MARKET_URL={url}  TEAM_KEY=****{key[-4:]}")
    if env.get("USE_LLM", "1") != "1":
        print("  ⚪ โหมด: กฎล้วน (กลยุทธ์ T26 ไม่เรียก LLM)")
    else:
        mode = "LLM ล้วน" if env.get("LLM_MODE") == "llm" else "กฎ + LLM"
        lurl = env.get("LLM_BASE_URL", "")
        if not lurl or "GATEWAY-ADDRESS" in lurl:
            print(f"  ❌ โหมด {mode} แต่ยังไม่ได้ใส่ LLM Base URL (ที่อยู่ gateway) -> ใส่ก่อน หรือเปลี่ยนเป็นกฎล้วน")
            return 1
        print(f"  🟡 โหมด: {mode} · gateway {lurl} · model {env.get('LLM_MODEL', 'event-llm')} · timeout {env.get('LLM_TIMEOUT', '60')} วิ"
              " (ใช้ได้เฉพาะ gateway ของงาน)")
    try:
        r = httpx.get(url.rstrip("/") + "/v1/state", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        st = r.json()
        if r.status_code >= 400:
            print(f"  ❌ ตลาดตอบ {r.status_code}: {st.get('error', st)}  (key ผิด/หมดอายุ?)")
            return 1
    except Exception as e:                                   # noqa: BLE001
        print(f"  ❌ ต่อตลาดไม่ได้: {e!r}  -> เช็ค WiFi และ MARKET_URL")
        return 1
    rnd = (st.get("round") or {}).get("round_id")
    print(f"  ✅ ตลาด: {st.get('market')}  ทีม {st.get('team_id')}  ช่วง {st.get('phase')}"
          f"{' ' + rnd if rnd else ''}  เหลือ {int(st.get('seconds_left') or 0)} วิ"
          f"  ส่ง appraisal แล้ว: {'ใช่' if st.get('appraisal_submitted') else 'ยัง'}")
    if st.get("capacity_left_kg") is not None:
        print(f"  ℹ️  มี capacity ต่อรอบ: {st.get('round_capacity_kg')} กก.")
    for l in st.get("lots", []):
        s = l["sample"]
        print(f"     {l['lot_id']}: {l['kg_available'] // 60} กระสอบ  เสีย {s['defect_pct']}%  ชื้น {s['moisture_pct']}%  ขนาด {s['screen']}")
    if st.get("market") == "sandbox":
        print("  ⚠️  นี่คือตลาดซ้อม (sandbox) ถ้าจะแข่งจริงต้องเปลี่ยน TEAM_KEY / MARKET_URL เป็นของวันแข่ง")

    # log เก่า
    old = last_market_in_logs()
    fresh = not st.get("appraisal_submitted")
    if fresh and ("--clean" in sys.argv or (old is not None and old != st.get("market"))):
        archive_logs(f"log เดิมเป็นของตลาด {old}" if old != st.get("market") else "สั่ง --clean")
    elif old is not None and not fresh:
        print("  ↩️  ส่ง appraisal แล้ว = รีสตาร์ทกลางเกม -> ใช้ log/ความจำเดิมต่อ")
    print("=== พร้อม ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
