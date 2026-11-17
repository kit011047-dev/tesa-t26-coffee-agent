# จำลองเกม 6 รอบแบบออฟไลน์ (ไม่แตะตลาด) เพื่อตรวจว่ากลยุทธ์เรียนรู้และยื่นซองถูกกติกา
import os, sys, random, json
sys.path.insert(0, os.path.expanduser("~/tesa-starter"))
import strategy
class Mem:
    def __init__(s): s.sent, s.rounds = {}, []
lots0 = [("LOT-T26-1", 540, dict(defect_pct=4.71, moisture_pct=11.55, screen=15)),
         ("LOT-T26-2", 120, dict(defect_pct=20.92, moisture_pct=10.56, screen=13)),
         ("LOT-T26-3", 300, dict(defect_pct=2.21, moisture_pct=12.5, screen=14)),
         ("LOT-T26-4", 420, dict(defect_pct=0.91, moisture_pct=13.33, screen=17))]
tv = {"LOT-T26-1": 114.35, "LOT-T26-2": 55.23, "LOT-T26-3": 122.04, "LOT-T26-4": 128.82}
C = dict(max_defect_pct=7.0, max_moisture_pct=12.5, min_screen=None)
I = dict(max_defect_pct=15.0, max_moisture_pct=14.0, min_screen=None)
CEIL = {"B3": 0.985, "B4": 1.00, "B5": 0.905, "B6": 0.88}      # เพดานลับสมมติ
COMP = {"B3": 0.96, "B4": 0.975, "B5": 0.87, "B6": 0.86}       # คู่แข่งยื่นถูกสุดราวนี้ (ได้ครึ่งหนึ่งของที่ต้องการ)
rules = {"timeline": {"rounds": 6}}
left = {l: kg for l, kg, _ in lots0}
mem, rev, errs = Mem(), 0.0, []
for rn in range(1, 7):
    rfqs = []
    for i, (b, t, sp, bags) in enumerate([("B3", "COMMERCIAL", C, 6), ("B4", "COMMERCIAL", C, 8), ("B5", "INSTANT", I, 6), ("B6", "INSTANT", I, 4)]):
        rfqs.append(dict(rfq_id=f"R{rn}-0{i+1}", buyer=b, buyer_id=b, buyer_type=t, kg=bags*60, bags=bags, spec=sp))
    state = dict(round=dict(round_id=f"R{rn}", kind="real", number=rn, of=6, calendar="ปกติ"), rfqs=rfqs,
                 lots=[dict(lot_id=l, kg_available=left[l], sample=s) for l, _, s in lots0], capacity_left_kg=None)
    ctx = strategy.Context(rules, [], tv, random.Random(rn), mem, None, 60)
    offers, src = strategy.decide(state, ctx)
    rmap = {r["rfq_id"]: r for r in rfqs}
    # ตรวจกติกา
    used = {}
    for o in offers:
        r = rmap[o["rfq_id"]]
        assert o["kg"] % 60 == 0 and o["kg"] > 0 and o["price_per_kg"] > 0
        used[o["lot_id"]] = used.get(o["lot_id"], 0) + o["kg"]
        assert strategy.spec_ok([s for l,_,s in lots0 if l==o["lot_id"]][0], r["spec"])
    for l, kg in used.items():
        assert kg <= left[l], (l, kg, left[l])
    mem.sent[f"R{rn}"] = [{**o, "buyer_type": rmap[o["rfq_id"]]["buyer_type"], "buyer_id": rmap[o["rfq_id"]]["buyer_id"]} for o in offers]
    # จับคู่แบบตลาด: ตัดเกินเพดาน, เรียงถูกสุดก่อน, คู่แข่งกินครึ่งหนึ่งที่ราคา COMP
    res = []
    for r in rfqs:
        b = r["buyer_id"]; cap = r["bags"] - r["bags"] // 2
        mine = sorted([o for o in offers if o["rfq_id"] == r["rfq_id"]], key=lambda o: o["price_per_kg"] / tv[o["lot_id"]])
        for o in mine:
            m = o["price_per_kg"] / tv[o["lot_id"]]
            if m > CEIL[b]: st, f = "ABOVE_LIMIT", 0
            elif cap <= 0 or m > COMP[b] + 0.03: st, f = "OUTBID", 0
            else:
                f = min(o["kg"] // 60, cap); cap -= f; st = "ACCEPTED" if f * 60 == o["kg"] else "PARTIAL"
            left[o["lot_id"]] -= f * 60; rev += f * 60 * o["price_per_kg"]
            res.append(dict(rfq_id=o["rfq_id"], lot_id=o["lot_id"], price_per_kg=o["price_per_kg"], status=st, filled_kg=f * 60))
    mem.rounds.append(dict(round_id=f"R{rn}", my_offers=res, market_summary=[]))
    print(f"R{rn}:", "; ".join(f"{r['rfq_id'][-2:]} {r['lot_id'][-1]} x{r['price_per_kg']/tv[r['lot_id']]:.3f} {r['status'][:6]} {r['filled_kg']//60}" for r in res))
    print("    ", [n for n in ctx.notes if n.startswith("learned")])
tvtot = sum(tv[l] * kg for l, kg, _ in lots0)
salv = sum(0.8 * tv[l] * kg for l, kg in left.items())
print("left bags", {l: kg // 60 for l, kg in left.items()}, "PR", round((rev + salv) / tvtot, 4))
