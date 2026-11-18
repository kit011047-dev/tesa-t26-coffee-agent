"""[T26] dashboard_bridge.py: ส่งความคิดของ agent ไปแสดงบน dashboard 7 ขั้นของทีม (~/tesa-market/part9)

main.py เรียกฟังก์ชันในไฟล์นี้ทีละขั้น แต่ละขั้นเขียน 1 บรรทัด JSON ลง events.jsonl
หน้าเว็บ dashboard (python -m part9.dashboard ใน ~/tesa-market) อ่านไฟล์นี้แล้วแสดงสด

    perception     รับรู้     : ล็อตของเรา + RFQ ของผู้ซื้อรอบนี้
    understanding  เข้าใจ     : TV ของแต่ละล็อต + ขายให้ผู้ซื้อรายไหนได้ / ไม่ได้เพราะอะไร
    decision       ตัดสินใจ   : ซองที่ยื่น ราคา ตัวคูณ × TV ราคาขั้นต่ำ
    action         ลงมือ      : ส่งซองสำเร็จ/ล้มเหลว
    learning       เรียนรู้   : ผลรอบก่อน + ตัวคูณที่ agent เรียนรู้ของผู้ซื้อแต่ละราย
    multi_agent    หลายเอเจนต์: ภาพรวมตลาด (ทุกทีมขายให้ผู้ซื้อแต่ละรายได้ราคาเท่าไร)
    governance     กำกับดูแล  : กฎที่ตรวจทุกซองก่อนส่ง

หลักสำคัญ: ทุกฟังก์ชันห่อ try/except  dashboard พังได้ แต่ agent ห้ามพังเพราะ dashboard
ปิดได้ด้วย DASHBOARD=0 ใน .env
"""
import json
import os
import time
from pathlib import Path

BAG = 60
SOLD = ("ACCEPTED", "PARTIAL")
STATUS_TH = {"ABOVE_LIMIT": "เกินเพดานผู้ซื้อ", "OUTBID": "แพ้ข้อเสนอที่คุ้มกว่า", "SPEC_MISMATCH": "ไม่ผ่านสเปก",
             "PARTIAL": "ขายได้บางส่วน", "OPEN": "รอผล"}

_path: Path | None = None
_seq = 0
_run = ""
_tv: dict = {}


def _enabled() -> bool:
    return os.environ.get("DASHBOARD", "1") != "0"


def _write(stage: str, round_no, data: dict) -> None:
    global _seq
    if not _enabled() or _path is None:
        return
    try:
        _seq += 1
        line = json.dumps({"run": _run, "seq": _seq, "t": round(time.time(), 3), "stage": stage,
                           "round": round_no, "data": data}, ensure_ascii=False, default=str)
        with _path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:                                  # noqa: BLE001
        print(f"  [dashboard] บันทึกไม่ได้: {e}")


def _safe(fn):
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:                              # noqa: BLE001 — ห้ามทำให้ agent ล่ม
            print(f"  [dashboard] {fn.__name__} error: {e!r}")
    return wrap


def _rnum(state: dict):
    r = state.get("round") or {}
    return r.get("number")


def _spec_reasons(sample: dict, spec: dict) -> list[str]:
    why = []
    if sample["defect_pct"] > spec["max_defect_pct"]:
        why.append(f"เมล็ดเสีย {sample['defect_pct']}% > {spec['max_defect_pct']}%")
    if sample["moisture_pct"] > spec["max_moisture_pct"]:
        why.append(f"ความชื้น {sample['moisture_pct']}% > {spec['max_moisture_pct']}%")
    if spec.get("min_screen") is not None and sample["screen"] < spec["min_screen"]:
        why.append(f"ขนาด {sample['screen']} < {spec['min_screen']}")
    return why


def _buyers(state: dict) -> dict:
    """รวม RFQ ตามผู้ซื้อ: buyer_id -> {name, type, spec, bags}"""
    out: dict = {}
    for r in state.get("rfqs", []):
        bid = r.get("buyer_id") or r["buyer"]
        b = out.setdefault(bid, {"name": r["buyer"], "type": r["buyer_type"], "spec": r["spec"], "bags": 0})
        b["bags"] += r["bags"]
    return out


# ---------------------------------------------------------------------
@_safe
def start(team: str, strategy_name: str, base_url: str, tv: dict) -> None:
    """เริ่มเกมใหม่ = ล้างไฟล์ events เดิม"""
    global _path, _seq, _run, _tv
    _path = Path(os.path.expanduser(os.environ.get("DASHBOARD_EVENTS", "~/tesa-market/part9/live/events.jsonl")))
    _path.parent.mkdir(parents=True, exist_ok=True)
    _path.write_text("", encoding="utf-8")
    _seq, _run, _tv = 0, time.strftime("%Y%m%d_%H%M%S"), dict(tv)
    _write("start", 0, {"team": team, "strategy": strategy_name, "base_url": base_url, "pi_enabled": False})


@_safe
def round_open(state: dict, rules: dict) -> None:
    """รอบใหม่เปิด: รับรู้ + เข้าใจ"""
    rn = _rnum(state)
    lots = [{"lot_id": l["lot_id"], "weight_kg": l["kg_available"], "moisture_pct": l["sample"]["moisture_pct"],
             "defect_pct": l["sample"]["defect_pct"], "source": "server"} for l in state["lots"]]
    bmap = _buyers(state)
    buyers = [{"id": bid, "name": f"{b['name']} · {b['type']}", "max_moisture": b["spec"]["max_moisture_pct"],
               "max_defect_pct": b["spec"]["max_defect_pct"], "quota": f"{b['bags']} กส.", "budget_remaining": None}
              for bid, b in bmap.items()]
    _write("perception", rn, {"max_rounds": rules.get("timeline", {}).get("rounds", 6), "lots": lots, "buyers": buyers})

    rows = []
    for l in state["lots"]:
        tv = _tv.get(l["lot_id"], 0)
        ok, no = [], {}
        for bid, b in bmap.items():
            why = _spec_reasons(l["sample"], b["spec"])
            (no.__setitem__(bid, why) if why else ok.append(bid))
        rows.append({"lot_id": l["lot_id"], "value_per_kg": tv, "weighted_defect_pct": l["sample"]["defect_pct"],
                     "moisture_factor": None, "total_value": round(tv * l["kg_remaining"]), "feasible": ok, "infeasible": no})
    _write("understanding", rn, {"lots": rows})


@_safe
def decision(state: dict, offers: list[dict], source: str, think_ms: float, min_markup: float) -> None:
    """ซองที่ agent เลือก + ตรวจกฎทุกซอง"""
    rn = _rnum(state)
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    lots = {l["lot_id"]: l for l in state["lots"]}
    rows, checks = [], []
    for o in offers:
        r, lot, tv = rfqs.get(o["rfq_id"], {}), lots.get(o["lot_id"], {}), _tv.get(o["lot_id"], 0)
        bid = r.get("buyer_id") or r.get("buyer", "?")
        rows.append({"lot_id": f"{o['lot_id']} ({o['kg'] // BAG} กส.)", "buyer_id": f"{bid} {o['rfq_id']}",
                     "price": o["price_per_kg"], "value": tv, "ratio": round(o["price_per_kg"] / tv, 3) if tv else None,
                     "p_win": None, "reserve": round(min_markup * tv, 2), "revenue_if_sold": round(o["price_per_kg"] * o["kg"])})
        lid = o["lot_id"]
        checks.append({"lot_id": lid, "rule": "ผ่านสเปกของ RFQ",
                       "ok": bool(r and lot) and not _spec_reasons(lot["sample"], r["spec"])})
        checks.append({"lot_id": lid, "rule": f"ราคา ≥ {min_markup} × TV", "ok": o["price_per_kg"] >= min_markup * tv - 0.01})
        checks.append({"lot_id": lid, "rule": "กก. เป็นทวีคูณของ 60", "ok": o["kg"] % BAG == 0 and o["kg"] > 0})
        checks.append({"lot_id": lid, "rule": "ไม่เกินที่ RFQ ต้องการ", "ok": bool(r) and o["kg"] <= r["kg"]})
    used: dict = {}
    for o in offers:
        used[o["lot_id"]] = used.get(o["lot_id"], 0) + o["kg"]
    for lid, kg in used.items():
        checks.append({"lot_id": lid, "rule": "รวมไม่เกินที่ล็อตเหลือ", "ok": kg <= lots.get(lid, {}).get("kg_available", 0)})
    held = [l["lot_id"] for l in state["lots"] if l["lot_id"] not in used and l["kg_available"] >= BAG]
    _write("decision", rn, {"strategy": source, "think_ms": round(think_ms, 1), "offers": rows, "held": held})
    _write("governance", rn, {"checks": checks, "notes": []})


@_safe
def action(state: dict, n_planned: int, n_sent: int, ms: float) -> None:
    rn = _rnum(state)
    _write("action", rn, {"ok": n_sent == n_planned, "n": n_sent, "ms": round(ms, 1)})
    if n_sent != n_planned:
        _write("governance", rn, {"checks": [], "notes": [{"level": "warn",
               "text": f"ส่งได้ {n_sent}/{n_planned} ซอง (ซองที่ผิดถูกข้าม ดู logs/decisions.jsonl)"}]})


@_safe
def learning(results: dict | None, memory, round_no=None) -> None:
    """ผลรอบที่เพิ่งปิด + สิ่งที่ agent เรียนรู้ + ภาพรวมตลาด"""
    if not results or not results.get("round_id"):
        return
    import strategy                                        # import ช้า: กัน import วนกัน
    rid = results["round_id"]
    rn = round_no if round_no is not None else int(str(rid).lstrip("R") or 0)
    sent = {(s["rfq_id"], s["lot_id"]): s for s in (memory.sent.get(rid, []) if memory else [])}
    mine = []
    for o in results.get("my_offers", []):
        s = sent.get((o["rfq_id"], o["lot_id"]), {})
        tv = _tv.get(o["lot_id"], 0)
        st = o["status"]
        mine.append({"lot_id": f"{o['lot_id']} ({o['kg'] // BAG} กส.)",
                     "buyer_id": f"{s.get('buyer_id', '?')} ×{o['price_per_kg'] / tv:.3f}" if tv else s.get("buyer_id", "?"),
                     "price": o["price_per_kg"], "status": "sold" if st in SOLD else st,
                     "reason": STATUS_TH.get(st, st), "p_win": None,
                     "revenue": round(o["price_per_kg"] * o.get("filled_kg", 0))})
    learned = strategy.learn(memory, _tv) if memory else {}
    types = {s.get("buyer_id"): s["buyer_type"] for ss in (memory.sent.values() if memory else []) for s in ss}
    knowledge = []
    for key, k in learned.items():
        if key.startswith("TYPE:"):
            continue
        t = types.get(key, "COMMERCIAL")
        knowledge.append({"buyer_id": f"{key} ({t[:4]})", "prior_r50": strategy.START.get(t, 1.0),
                          "r50": round(k["m"], 3) if k["m"] else None,
                          "max_paid": round(k["best"], 3) if k["best"] else None, "n_obs": k["n"]})
    _write("learning", rn, {"mine": mine, "knowledge": knowledge})
    # ตลาดจริงไม่บอกว่าทีมไหนขายอะไร มีแต่ภาพรวมต่อ RFQ -> แสดงเป็นตารางภาพรวม (summary=True)
    trades = [{"rfq_id": m["rfq_id"], "buyer": m["buyer"], "buyer_type": m["buyer_type"], "offers": m["offers"],
               "wanted_bags": m["wanted_kg"] // BAG, "bought_bags": m["bought_kg"] // BAG,
               "price_min": m.get("price_min"), "price_max": m.get("price_max"), "price_avg": m.get("price_avg")}
              for m in results.get("market_summary", [])]
    _write("multi_agent", rn, {"trades": trades, "summary": True})


@_safe
def llm_round(state: dict, info: dict, source: str, offers: list[dict], llm) -> None:
    """[T26] สิ่งที่ LLM เห็นและตอบในรอบนี้ ไว้ปรับจูนค่า LLM"""
    import strategy
    info = info or {}
    final = {(o["rfq_id"], o["lot_id"], round(o["price_per_kg"], 2)) for o in offers}
    rows, used = [], 0
    for o in info.get("offers", []):
        tv = _tv.get(o["lot_id"], 0)
        m = min(max(o["markup"], strategy.MIN_MARKUP), strategy.MAX_MARKUP)
        hit = (o["rfq_id"], o["lot_id"], round(m * tv, 2)) in final
        used += hit
        rows.append({**o, "markup_clamped": round(m, 3), "used": hit,
                     "diff": round(o["markup"] - o["rule_markup"], 3) if o.get("rule_markup") else None})
    _write("llm", _rnum(state), {
        "mode": info.get("mode", strategy.LLM_MODE), "source": source, "ok": info.get("ok", False),
        "skipped": not info, "seconds": info.get("seconds"), "timeout": info.get("timeout"),
        "usage": info.get("usage"), "error": info.get("error"), "reason": info.get("reason"),
        "n_candidates": info.get("n_candidates"), "target_bags": info.get("target_bags"),
        "offers": rows, "used": used, "n_final": len(offers),
        "settings": {"model": getattr(llm, "model", None),
                     "temperature": getattr(llm, "temperature", None), "max_tokens": getattr(llm, "max_tokens", None),
                     "thinking": getattr(llm, "thinking", None), "history": strategy.LLM_HISTORY,
                     "extra_prompt": bool(strategy.EXTRA_PROMPT.strip())},
        "system": (info.get("system") or "")[-3000:], "user": (info.get("user") or "")[:6000],
        "raw": info.get("raw")})


@_safe
def end(standings: dict, score: dict | None = None) -> None:
    sc = {"revenue": standings.get("revenue_thb")}
    if score:
        sc["score"] = score.get("total")
        sc["pr"] = score.get("pr")
        sc["rank"] = score.get("rank")
    _write("end", None, {"score": sc})
