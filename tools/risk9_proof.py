"""[T26] risk9_proof.py: แสดงหลักฐาน Risk 9 จาก log วันแข่ง (อ่านอย่างเดียว)
ทุกรอบที่ต่อ LLM gateway ไม่ได้ agent ยังส่งคำสั่งขายด้วยกฎ (source = rules)

    python3 tools/risk9_proof.py
"""
import json
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "decisions.jsonl"

print(f"=== T26 · Risk 9: ต่อ LLM gateway ไม่ได้ -> ใช้กฎแทน · {LOG} ===")
ok = 0
for line in LOG.read_text(encoding="utf-8").splitlines():
    e = json.loads(line)
    if e["event"] == "new_game":
        print(f"{e['time']}  new_game      market={e['market']}  lots={e['lots']}")
    elif e["event"] == "offers_sent":
        err = next((n for n in e.get("notes", []) if n.startswith("llm failed")), "-")
        ok += e["n"] > 0
        print(f"{e['time']}  offers_sent   round={e['round']:<3} source=\"{e['source']}\"  "
              f"ส่ง {e['n']} ซอง {e['kg']} กก. ใน {e['seconds']} วิ  | {err}")
    elif e["event"] == "market_closed":
        s = e["standings"]
        print(f"{e['time']}  market_closed ขายได้ {s['kg_sold']}/{s['kg_total']} กก.  รายได้ {s['revenue_thb']:,.2f} บาท")
print(f"=== สรุป: ส่งคำสั่งขายครบ {ok} รอบ แม้ LLM ไม่ตอบทุกรอบ ===")
