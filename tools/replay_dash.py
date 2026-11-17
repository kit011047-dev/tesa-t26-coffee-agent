# เล่นเกมที่จบแล้วซ้ำผ่าน dashboard_bridge (ไม่แตะตลาด) เพื่อทดสอบ dashboard
import json, os, sys, glob, time
os.chdir(os.path.expanduser("~/tesa-starter")); sys.path.insert(0, ".")
import dashboard_bridge as dash, strategy
from memory import Memory
f = sorted(glob.glob("logs/memory-*.json"), key=os.path.getmtime)[-1]
full = Memory(f); mem = Memory(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_nomem.json')); mem.save = lambda: None
ev = [json.loads(l) for l in open("logs/decisions.jsonl")]
start = max(i for i, e in enumerate(ev) if e["event"] == "new_game"); tv = ev[start]["tv"]
samples = {"LOT-T26-1": (18.99, 15.13, 14, 420), "LOT-T26-2": (1.96, 11.97, 15, 540), "LOT-T26-3": (2.47, 11.63, 13, 180), "LOT-T26-4": (5.65, 10.7, 16, 180)}
left = {k: v[3] for k, v in samples.items()}
dash.start("T26", "T26 ขั้นบันได + เรียนรู้รายผู้ซื้อ (replay)", "replay", tv)
delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0
prev = None
for res in full.rounds:
    rid = res["round_id"]; n = int(rid[1:])
    rfqs = [dict(rfq_id=m["rfq_id"], buyer=m["buyer"], buyer_id="B" + m["buyer"].split()[0], buyer_type=m["buyer_type"],
                 kg=m["wanted_kg"], bags=m["wanted_kg"] // 60,
                 spec=dict(max_defect_pct=15.0 if m["buyer_type"] == "INSTANT" else 7.0, max_moisture_pct=14.0 if m["buyer_type"] == "INSTANT" else 12.5, min_screen=None))
            for m in res["market_summary"]]
    state = dict(round=dict(round_id=rid, number=n), rfqs=rfqs,
                 lots=[dict(lot_id=k, kg_available=left[k], kg_remaining=left[k], sample=dict(defect_pct=v[0], moisture_pct=v[1], screen=v[2])) for k, v in samples.items()])
    if prev: mem.rounds.append(prev); dash.learning(prev, mem)
    dash.round_open(state, {"timeline": {"rounds": 6}})
    offers = full.sent.get(rid, [])
    dash.decision(state, offers, "rules", 0.4, strategy.MIN_MARKUP)
    dash.action(state, len(offers), len(offers), 120)
    mem.sent[rid] = offers
    for o in res["my_offers"]: left[o["lot_id"]] -= o.get("filled_kg", 0)
    prev = res; time.sleep(delay)
mem.rounds.append(prev); dash.learning(prev, mem)
dash.end({"revenue_thb": 108840.6}, {"total": 100.0, "pr": 0.9871, "rank": 1})
print("events:", sum(1 for _ in open(os.path.expanduser("~/tesa-market/part9/live/events.jsonl"))))
