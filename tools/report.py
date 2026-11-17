# สรุปเกมปัจจุบัน: ซองที่ยื่น (ตัวคูณ) + ผล + ยอดขาย  (อ่านอย่างเดียว)
import json, os, glob, sys
os.chdir(os.path.expanduser("~/tesa-starter"))
import httpx
env = dict(l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.startswith("#"))
h = {"Authorization": "Bearer " + env["TEAM_KEY"]}
st = httpx.get(env["MARKET_URL"] + "/v1/state", headers=h, timeout=8).json()
ev = [json.loads(l) for l in open("logs/decisions.jsonl")]
start = max(i for i, e in enumerate(ev) if e["event"] == "new_game")
tv = ev[start]["tv"]
mem = json.load(open(sorted(glob.glob("logs/memory-*.json"), key=os.path.getmtime)[-1]))
res = {r["round_id"]: r for r in mem["rounds"]}
if st.get("last_results") and st["last_results"].get("round_id"):
    res.setdefault(st["last_results"]["round_id"], st["last_results"])
types = {}
for rid, offs in mem["sent"].items():
    for o in offs: types[o["rfq_id"]] = (o.get("buyer_id"), o["buyer_type"][:4])
print("phase", st["phase"], (st["round"] or {}).get("round_id"), int(st["seconds_left"] or 0), "s | appraisal", st["appraisal_submitted"])
for e in ev[start:]:
    if e["event"] != "offers_sent": 
        if e["event"] not in ("new_game",): print(e["time"][11:16], e["event"], {k: v for k, v in e.items() if k not in ("time", "event", "tv")})
        continue
    rid = e["round"]; r = res.get(rid)
    out = {(o["rfq_id"], o["lot_id"], o["price_per_kg"]): o for o in (r or {}).get("my_offers", [])}
    print(f"{rid}:")
    for o in e["offers"]:
        x = out.get((o["rfq_id"], o["lot_id"], o["price_per_kg"]))
        b = types.get(o["rfq_id"], ("?", "?"))
        print(f"   {o['lot_id'][-1]} → {o['rfq_id']} {b[0]}/{b[1]} {o['kg']//60}กส. x{o['price_per_kg']/tv[o['lot_id']]:.3f} = {o['price_per_kg']}  "
              + (f"{x['status']} ขายได้ {x.get('filled_kg',0)//60}" if x else "(รอผล)"))
    for n in e.get("notes", []):
        if n.startswith("learned"): print("   ", n)
s = st["standings"]
print("ยอดขาย", s["revenue_thb"], "บาท | ขายแล้ว", s["kg_sold"]//60, "กส. | เหลือ", s["kg_remaining"]//60, "กส. | เฉลี่ย", s["avg_price_per_kg"])
