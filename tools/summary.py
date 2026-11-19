"""[T26] summary.py: สรุปตัวชี้วัดระหว่างแข่ง อ่านอย่างเดียว ไม่ส่งคำสั่งไปตลาด (ยกเว้น --live อ่าน /v1/state)

  .venv/bin/python tools/summary.py            # เกมล่าสุด (ไฟล์ความจำใหม่สุดใน logs/)
  .venv/bin/python tools/summary.py --live     # + ของเหลือ/ช่วงตลาดตอนนี้ จาก /v1/state
  .venv/bin/python tools/summary.py FILE.json  # ระบุไฟล์ความจำเอง

ตัวชี้วัดและเกณฑ์เตือนตามไฟล์ docs/แผนปรับราคา-ตลาดแข่ง-20พย.md
"""
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SOLD = ("ACCEPTED", "PARTIAL")
BAG = 60


def load_memory(path: str | None) -> tuple[str, dict]:
    f = path or max(glob.glob(str(HERE / "logs" / "memory-*.json")), key=os.path.getmtime)
    return f, json.loads(Path(f).read_text(encoding="utf-8"))


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:4.0f}%" if b else "   - "


def zero_rounds() -> list[str]:
    """รอบที่ส่ง 0 ซอง ตั้งแต่เกมล่าสุดเริ่ม (จาก decisions.jsonl)"""
    p = HERE / "logs" / "decisions.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    start = max((i for i, r in enumerate(rows) if r.get("event") == "new_game"), default=0)
    out = []
    for r in rows[start:]:
        if r.get("event") == "offers_sent" and r.get("n") == 0:
            out.append(f"{r['round']} ({r.get('source', '?')})")
        if r.get("event") in ("batch_rejected", "offer_rejected") and "ROUND_CLOSED" in json.dumps(r, ensure_ascii=False):
            out.append(f"ROUND_CLOSED @ {r['time'][11:19]}")
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path, mem = load_memory(args[0] if args else None)
    print(f"ไฟล์ความจำ: {os.path.relpath(path, HERE)}\n")

    by_type = defaultdict(lambda: defaultdict(int))        # ประเภท -> สถานะ -> จำนวนซอง
    best = {}                                              # ประเภท -> ตัวคูณสูงสุดที่ขายได้
    rows = []
    for rnd in mem["rounds"]:
        rid = rnd["round_id"]
        sent = {(s["rfq_id"], s["lot_id"], round(s["price_per_kg"], 2)): s for s in mem["sent"].get(rid, [])}
        st = defaultdict(int)
        kg_sent = kg_sold = 0
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"], round(o["price_per_kg"], 2)))
            t = (s or {}).get("buyer_type", "ไม่ทราบ")   # ไม่ทราบ = ซองที่ส่งก่อน agent รีสตาร์ท (ไม่มีบันทึกการส่งในไฟล์นี้)
            st[o["status"]] += 1
            by_type[t][o["status"]] += 1
            kg_sent += o["kg"]
            kg_sold += o.get("filled_kg") or 0
            if o["status"] in SOLD and s and s.get("markup"):
                best[t] = max(best.get(t, 0), s["markup"])
        rows.append((rid, rnd.get("kind", ""), len(rnd.get("my_offers", [])), kg_sent, kg_sold, dict(st)))

    print("รายรอบ")
    print(f"{'รอบ':5s} {'ซอง':>4s} {'ยื่น กก.':>8s} {'ขาย กก.':>8s}  สถานะ")
    for rid, kind, n, ks, kd, st in rows:
        tag = " (ซ้อม)" if kind == "practice" else ""
        print(f"{rid:5s} {n:4d} {ks:8d} {kd:8d}  {st}{tag}")

    print("\nแยกประเภทผู้ซื้อ (ทุกรอบรวมกัน)")
    print(f"{'ประเภท':11s} {'ซอง':>4s} {'ขายได้':>7s} {'OUTBID':>7s} {'เกินเพดาน':>9s} {'สเปกไม่ผ่าน':>11s} {'ตัวคูณสูงสุดที่ขายได้':>20s}")
    alerts = []
    for t, c in sorted(by_type.items()):
        n = sum(c.values())
        sold, out, over, spec = sum(c[s] for s in SOLD), c["OUTBID"], c["ABOVE_LIMIT"], c["SPEC_MISMATCH"]
        print(f"{t:11s} {n:4d} {pct(sold, n):>7s} {pct(out, n):>7s} {pct(over, n):>9s} {pct(spec, n):>11s} {best.get(t, 0) or '-':>20}")
        if n >= 6:
            if out / n > 0.40:
                alerts.append(f"{t}: OUTBID {pct(out, n).strip()} > 40% → พิจารณาลด START −0.02")
            if over / n > 0.10:
                alerts.append(f"{t}: เกินเพดาน {pct(over, n).strip()} > 10% → พิจารณาลด START −0.03")
            if sold / n >= 0.85:
                alerts.append(f"{t}: ขายได้ {pct(sold, n).strip()} ≥ 85% → อาจทิ้งเงิน พิจารณาขึ้น START +0.02 (ดูว่าขายที่ราคาบนสุดไหม)")

    z = zero_rounds()
    print("\nรอบที่ส่ง 0 ซอง / ROUND_CLOSED:", ", ".join(z) if z else "ไม่มี")
    if z:
        alerts.append("มีรอบส่ง 0 ซองหรือ ROUND_CLOSED → ดู log หาเหตุ (ของหมด / สเปก / capacity / ช้าไป)")

    if "--live" in sys.argv:
        for l in (HERE / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in l and not l.startswith("#"):
                k, v = l.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        sys.path.insert(0, str(HERE))
        from market import Market
        s = Market(os.environ["MARKET_URL"], os.environ["TEAM_KEY"]).state()
        total = sum(x["kg_total"] for x in s["lots"])
        left = sum(x["kg_remaining"] for x in s["lots"])
        rnd = (s.get("round") or {}).get("round_id", "-")
        print(f"\nสด: ตลาด={s['market']} ช่วง={s['phase']} รอบ={rnd} เหลือ {s['seconds_left']} วิ · ของเหลือ {left}/{total} กก. ({pct(left, total).strip()})")
        if s["phase"] == "ROUND_OPEN" and (s.get("round") or {}).get("number", 0) >= 8 and total and left / total > 0.5:
            alerts.append("ผ่านครึ่งเกมแล้วของเหลือ > 50% → ตรวจว่าเหลือเป็นล็อตที่ขายไม่ได้เลยหรือยัง (ถ้าใช่ ไม่ต้องทำอะไร)")

    print("\nข้อควรดู:" if alerts else "\nไม่มีเกณฑ์เตือน")
    for a in alerts:
        print(" -", a)


if __name__ == "__main__":
    main()
