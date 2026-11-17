"""Decide this round's offers.  <- ไฟล์หลักที่ทีมควรแก้

Every round:
  1. candidates()  every (lot, RFQ) pair whose sample meets the RFQ spec
  2. llm_plan()    the LLM picks offers from those candidates: which, how many bags, what markup
  3. rule_plan()   used instead when the LLM is off, too slow, or answers nonsense
  4. guard()       every offer, from the LLM or the rules, is checked before it is sent

price_per_kg = markup × our TV estimate. A markup of 1.00 asks exactly what we think the lot is worth.
"""
import json
import math
import random
from dataclasses import dataclass, field

BAG = 60
# Markup per buyer type. The starter asks 1.00 × TV from everyone.
# TODO: buyer types do not pay the same (Specialty pays the most, Instant the least), and these never change.
#       Price each type differently and move the markups after each round's results
#       (ACCEPTED -> ask a bit more, OUTBID / ABOVE_LIMIT -> ask less) — จุดนี้ปรับแล้วได้ผลมากที่สุด
MARKUP = {"SPECIALTY": 1.00, "COMMERCIAL": 1.00, "INSTANT": 1.00}
MIN_MARKUP, MAX_MARKUP = 0.85, 1.30   # unsold beans go to salvage at 0.80 × TV, so never go near that
PACE = 1.6                            # [T26] จูนด้วย tools/sim2.py: 1.3 -> 1.6 (ยื่นต่อรอบมากขึ้น ของไม่กองรอบท้าย)

# ===== [T26] กลยุทธ์ทีม: ขั้นบันไดราคา + เรียนรู้เพดานแยกผู้ซื้อแต่ละราย =====
# ตัวคูณเริ่มต้น จากผลรอบเก็บคะแนน 17 พ.ย. (Commercial ขายได้ 0.99 × TV; sandbox รับที่ 1.00 ได้บ้าง)
# Instant ปฏิเสธที่ 1.00 ทุกซอง และไม่รับที่ 0.92 -> ขั้นบันได 0.92 / 0.89 / 0.86
START = {"SPECIALTY": 1.05, "COMMERCIAL": 1.00, "INSTANT": 0.92}
LADDER = (0.0, -0.03, -0.06)          # ผู้ซื้อที่ยังไม่รู้จัก: ยื่น 3 ราคาพร้อมกันในรอบเดียว
STEP_UP = 0.01                        # ขายได้       -> รอบหน้าขยับขึ้น
STEP_OUTBID = 0.01                    # OUTBID       -> ลดนิดเดียว (ใกล้แล้ว แค่แพ้คู่แข่ง)
STEP_LIMIT = 0.03                     # ABOVE_LIMIT  -> ลดมากกว่า (เกินเพดานลับ)
PROBE = 0.02                          # ผู้ซื้อที่รู้จักแล้ว: แบ่ง 1 กระสอบลองราคาสูงกว่า
ENDGAME = {2: -0.01, 1: -0.04}        # รอบที่เหลือ -> ลดเพิ่มเพื่อเร่งขาย (รอบสุดท้ายขายไม่ออก = 0.80 จึงลดได้มาก แต่ไม่ต่ำกว่า MIN_MARKUP)
OUTBID_ACCEL = True                   # แพ้คู่แข่งติดกันหลายรอบ -> ถอยแรงขึ้น (0.01, 0.02, 0.03, ...)
OUTBID_MAX_STEP = 0.03
SPREAD = 3                            # แบ่งกระสอบของล็อตหนึ่งไปได้ถึง 3 RFQ ต่อรอบ (กระจายความเสี่ยงแพ้ใบเดียว)
CAP_ORDER = "tv"                      # capacity จำกัด: "tv" = ล็อตแพงก่อน | "margin" = กำไรเหนือ 0.80×TV มากสุดก่อน
CAP_PRESSURE = 0.02                   # capacity ไม่พอขายหมด -> ลดราคาเพิ่มเท่านี้
RAISE_STRICT = False                 # True = ขายได้แต่มีซองแพ้ที่ราคาเท่ากัน -> ไม่ขยับขึ้น
SOLD = ("ACCEPTED", "PARTIAL")


@dataclass
class Context:
    rules: dict                       # GET /v1/calendar -> rules
    calendar: list                    # GET /v1/calendar -> calendar
    tv: dict                          # lot_id -> our TV estimate (baht/kg)
    rng: random.Random
    memory: object = None
    llm: object = None
    seconds_left: float = 999         # time left in this round when the state was read
    notes: list = field(default_factory=list)


def spec_ok(sample: dict, spec: dict) -> bool:
    # TODO: the sample is noisy. A lot just inside the limit may really be outside -> SPEC_MISMATCH.
    #       ลองเว้น margin เล็กน้อยสำหรับล็อตที่ค่าใกล้ขอบสเปก
    return (sample["defect_pct"] <= spec["max_defect_pct"] and sample["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], ctx.rules["timeline"]["rounds"]
    return total if rnd["kind"] == "practice" else total - rnd["number"] + 1


def sell_budget(state: dict, ctx: Context) -> dict[str, int]:
    """How many kg of each lot to offer this round: spread sales over the rounds that are left."""
    # TODO: use the Market Calendar — sell more in festival rounds (Specialty pays well), less when demand is low
    left = rounds_left(state, ctx)
    out = {}
    for lot in state["lots"]:
        avail = lot["kg_available"] // BAG * BAG
        share = avail if left <= 1 else math.ceil(avail / left * PACE / BAG) * BAG
        out[lot["lot_id"]] = min(avail, max(BAG, share)) if avail >= BAG else 0
    return out


def candidates(state: dict, ctx: Context) -> list[dict]:
    budget = sell_budget(state, ctx)
    out = []
    for lot in state["lots"]:
        if budget[lot["lot_id"]] < BAG:
            continue
        for rfq in state["rfqs"]:
            if spec_ok(lot["sample"], rfq["spec"]):
                out.append({"lot_id": lot["lot_id"], "rfq_id": rfq["rfq_id"], "buyer": rfq["buyer"],
                            "buyer_id": rfq.get("buyer_id") or rfq["buyer"],
                            "buyer_type": rfq["buyer_type"], "rfq_bags": rfq["bags"],
                            "max_bags": min(budget[lot["lot_id"]], rfq["kg"]) // BAG,
                            "tv": ctx.tv[lot["lot_id"]]})
    # Shuffle (weighted by RFQ size): if 40 teams all chase the first RFQ, they all get OUTBID.
    keyed = [(ctx.rng.random() ** (1 / c["rfq_bags"]), c) for c in out]
    return [c for _, c in sorted(keyed, key=lambda x: -x[0])]


def learn(memory, tv: dict) -> dict:
    """[T26] อ่านผลทุกรอบที่ผ่านมา -> ตัวคูณที่ควรยื่นให้ผู้ซื้อแต่ละราย (และแต่ละประเภท)

    key = buyer_id หรือ "TYPE:<ประเภท>"  ->  {"m": ตัวคูณรอบหน้า, "ceil": ABOVE_LIMIT ต่ำสุดที่เคยเจอ, "best": ขายได้สูงสุด, "n": จำนวนซอง}
    """
    out: dict = {}
    if memory is None:
        return out
    for rnd in memory.rounds:
        sent = memory.sent.get(rnd["round_id"], [])
        info = {(s["rfq_id"], s["lot_id"]): s for s in sent}
        groups: dict = {}
        for r in rnd.get("my_offers", []):
            s = info.get((r["rfq_id"], r["lot_id"]))
            if not s or r["lot_id"] not in tv or r.get("status") == "SPEC_MISMATCH":
                continue
            m = r["price_per_kg"] / tv[r["lot_id"]]           # ตัวคูณจริงของซองนี้ (ขั้นบันไดคนละราคา)
            for key in filter(None, (s.get("buyer_id"), "TYPE:" + s["buyer_type"])):
                groups.setdefault(key, []).append((m, r["status"]))
        for key, obs in groups.items():
            sold = [m for m, st in obs if st in SOLD]
            over = [m for m, st in obs if st == "ABOVE_LIMIT"]
            lost = [m for m, st in obs if st == "OUTBID"]
            k = out.setdefault(key, {"m": None, "ceil": 9.0, "best": None, "n": 0, "streak": 0})
            k["n"] += len(obs)
            if over:
                k["ceil"] = min(k["ceil"], min(over))
            if sold:
                k["streak"] = 0
                k["best"] = max(sold + ([k["best"]] if k["best"] else []))
                # ขายได้ทุกขั้นที่ยื่น -> ยังมีที่ว่างข้างบน ขยับขึ้น / มีขั้นที่สูงกว่าแล้วไม่ได้ -> คงราคาที่ขายได้
                top = max(sold)
                if RAISE_STRICT:   # ขยับขึ้นเฉพาะเมื่อไม่มีซองไหนแพ้/เกินเพดาน ที่ราคาเท่านี้หรือสูงกว่า
                    blocked = any(m >= top - 1e-9 for m, st in obs if st not in SOLD)
                else:
                    blocked = top < max(m for m, _ in obs) - 1e-9
                nxt = top if blocked else top + STEP_UP
            elif lost:
                k["streak"] += 1
                step = min(STEP_OUTBID * k["streak"], OUTBID_MAX_STEP) if OUTBID_ACCEL else STEP_OUTBID
                nxt = min(lost) - step
            elif over:
                nxt = min(over) - STEP_LIMIT
                if k["best"] and k["best"] < min(over):        # เคยขายได้ต่ำกว่าเพดาน -> กลับไปราคานั้น ไม่ลดเกิน
                    nxt = max(nxt, k["best"])
            else:
                continue
            k["m"] = min(nxt, k["ceil"] - 0.01)               # ไม่ยื่นชนเพดานที่รู้แล้วซ้ำ
    return out


def markup_for(c: dict, learned: dict, rl: int) -> tuple[float, bool, float]:
    """[T26] (ตัวคูณที่จะยื่น, รู้จักผู้ซื้อรายนี้แล้วหรือยัง, เพดานที่รู้แล้ว)"""
    k = learned.get(c["buyer_id"])
    known = bool(k and k["m"])
    ceil = k["ceil"] if k else 9.0
    if not known:                                          # ยังไม่เคยยื่นให้รายนี้ -> ใช้ค่าของประเภทเดียวกัน
        k = learned.get("TYPE:" + c["buyer_type"])
    m = k["m"] if k and k["m"] else START.get(c["buyer_type"], 1.0)
    m += ENDGAME.get(rl, 0.0)
    return min(max(m, MIN_MARKUP), MAX_MARKUP), known, ceil


def split_bags(bags: int, m: float, known: bool, ceil: float = 9.0) -> list[tuple[int, float]]:
    """[T26] แบ่งกระสอบเป็นหลายราคา: ยังไม่รู้จัก = ขั้นบันได 3 ราคา, รู้จักแล้ว = ราคาหลัก + ซองลองราคาสูง 1 กระสอบ
    ซองลองราคาไม่เกิน (เพดานที่เคยโดน ABOVE_LIMIT − 0.01) ไม่ยื่นชนเพดานเดิมซ้ำ"""
    probe = min(m + PROBE, ceil - 0.01)
    if known:
        parts = [(1, probe), (bags - 1, m)] if bags >= 2 and probe > m + 1e-9 else [(bags, m)]
    elif bags == 1:
        parts = [(1, m)]
    elif bags == 2:
        parts = [(1, m + LADDER[0]), (1, m + LADDER[1])]
    else:
        parts = [(1, m + LADDER[0]), (bags - 2, m + LADDER[1]), (1, m + LADDER[2])]
    merged: dict = {}
    for n, mk in parts:
        mk = round(min(max(mk, MIN_MARKUP), MAX_MARKUP), 3)
        if n > 0:
            merged[mk] = merged.get(mk, 0) + n
    return [(n, mk) for mk, n in sorted(merged.items(), reverse=True)]


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """[T26] ล็อตแพงก่อน, ผู้ซื้อที่จ่ายดีก่อน, ราคาตามที่เรียนรู้ + ขั้นบันได"""
    budget = sell_budget(state, ctx)
    rl = rounds_left(state, ctx)
    learned = learn(ctx.memory, ctx.tv)
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    # ล็อตที่เข้าสเปก Commercial/Specialty ได้ ไม่ขาย Instant (จ่ายน้อยกว่า) จนกว่าจะเหลือ 2 รอบสุดท้าย
    premium_ok = {c["lot_id"] for c in cands if c["buyer_type"] != "INSTANT"}
    pool = [c for c in cands if not (c["buyer_type"] == "INSTANT" and c["lot_id"] in premium_ok and rl > 2)]
    price = {id(c): markup_for(c, learned, rl) for c in pool}
    # [T26] capacity ต่อรอบ (วันที่ 18, 20): ถ้าของที่ขายได้เหลือมากกว่า capacity ที่เหลือทั้งเกม -> ยอมลดราคา
    cap = state.get("capacity_left_kg")
    if cap is not None and CAP_PRESSURE:
        sellable = sum(l["kg_available"] for l in state["lots"] if l["lot_id"] in {c["lot_id"] for c in cands})
        if sellable > cap * rl:
            for k, (m, known, ceil) in price.items():
                price[k] = (max(m - CAP_PRESSURE, MIN_MARKUP), known, ceil)
    pool.sort(key=lambda c: (-c["tv"], -price[id(c)][0]))   # sort แบบ stable: RFQ ที่ราคาเท่ากันยังคงลำดับสุ่ม
    plan = []
    # กระจาย: ล็อตหนึ่งยื่นได้หลาย RFQ ต่อรอบ (ใบละไม่เกิน budget / SPREAD) ถ้าแพ้ใบหนึ่งยังมีลุ้นอีกใบ
    per_rfq = {l: max(1, math.ceil(b / BAG / SPREAD)) for l, b in budget.items()}
    for c in pool:
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]], per_rfq[c["lot_id"]])
        if bags <= 0:
            continue
        m, known, ceil = price[id(c)]
        for n, mk in split_bags(bags, m, known, ceil):
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": n, "markup": mk})
        budget[c["lot_id"]] -= bags * BAG
        rfq_left[c["rfq_id"]] -= bags
    if cap is not None and CAP_ORDER == "margin":
        # capacity จำกัด: guard ตัดตามลำดับ -> ให้ซองที่กำไรเหนือ 0.80×TV (ขายไม่ออก) มากที่สุดได้ช่องก่อน
        tvs = ctx.tv
        plan.sort(key=lambda p: -(p["markup"] - 0.80) * tvs.get(p["lot_id"], 0))
    if learned:
        ctx.notes.append("learned: " + ", ".join(f"{k}={v['m']:.3f}" for k, v in learned.items() if v["m"]))
    return plan


SYSTEM = """You are the sales agent of a coffee farm selling green robusta beans in a sealed-offer market.
Each round buyers post RFQs. You choose which RFQs to offer your lots to, how many 60 kg bags, and a markup.
price = markup x the lot's True Value estimate (tv). Buyers rank offers by price / true value, cheapest first,
and reject offers above their hidden ceiling. Unsold beans are worth only 0.80 x tv at the end.
Buyer types pay differently: SPECIALTY the most, then COMMERCIAL, INSTANT the least. Ceilings are hidden;
learn them from the results (ABOVE_LIMIT means the price was too high for that buyer).
Answer only JSON matching the schema."""

SCHEMA = {"type": "object", "required": ["offers", "reason"], "properties": {
    "offers": {"type": "array", "items": {"type": "object", "required": ["candidate", "bags", "markup"], "properties": {
        "candidate": {"type": "integer"}, "bags": {"type": "integer"}, "markup": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 300}}}   # short: a long reason can run past max_tokens and cut the JSON


def llm_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict] | None:
    rnd = state["round"]
    target = sum(sell_budget(state, ctx).values()) // BAG
    if state["capacity_left_kg"] is not None:
        target = min(target, state["capacity_left_kg"] // BAG)
    time_for_llm = ctx.seconds_left - 5          # keep 5 s to send the offers before the round closes
    if time_for_llm < 3:
        ctx.notes.append("no time for the LLM this round")
        return None
    table = [{"candidate": i, **{k: c[k] for k in ("lot_id", "buyer", "buyer_type", "rfq_bags", "max_bags", "tv")}}
             for i, c in enumerate(cands)]
    user = "\n".join([
        f"Round {rnd['round_id']} ({rnd['kind']}), market calendar: {rnd['calendar']}. Rounds left: {rounds_left(state, ctx)}.",
        f"You may offer at most {state['capacity_left_kg'] // BAG if state['capacity_left_kg'] is not None else 'any'} bags in total this round.",
        f"Plan to offer about {target} bags in total this round: unsold beans lose 20% of their value at the end.",
        f"Suggested markups: {json.dumps(MARKUP)}",
        "Recent results:", ctx.memory.summary() if ctx.memory else "none",
        "Candidates (lot x RFQ, already spec-checked):", json.dumps(table, ensure_ascii=False),
        "Pick offers. Use each candidate at most once; bags <= max_bags.",
    ])
    # TODO: give the LLM more to reason about — buyer personas' habits, the calendar ahead, price trends
    answer = ctx.llm.ask_json(SYSTEM, user, SCHEMA, timeout=time_for_llm)
    if answer is None or not isinstance(answer.get("offers"), list):
        ctx.notes.append(f"llm failed: {ctx.llm.last_error}")
        return None
    plan = []
    for o in answer["offers"]:
        try:
            c = cands[int(o["candidate"])]
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": int(o["bags"]), "markup": float(o["markup"])})
        except (KeyError, IndexError, TypeError, ValueError):
            ctx.notes.append(f"llm bad offer ignored: {o}")
    ctx.notes.append(f"llm reason: {str(answer.get('reason'))[:200]}")
    return plan


def guard(state: dict, ctx: Context, plan: list[dict]) -> list[dict]:
    """Turn a plan into offers the market will accept. Never trust the LLM's numbers as they are."""
    lots = {l["lot_id"]: l for l in state["lots"]}
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    room = state["capacity_left_kg"] if state["capacity_left_kg"] is not None else 10**9
    lot_left = {i: l["kg_available"] for i, l in lots.items()}
    rfq_left = {i: r["kg"] for i, r in rfqs.items()}
    offers, seen = [], set()
    for p in plan:
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        key = (p["lot_id"], p["rfq_id"], round(float(p.get("markup", 0)), 3))   # [T26] ขั้นบันได: ล็อต×RFQ เดียวกันได้หลายราคา
        if lot is None or rfq is None or key in seen or not spec_ok(lot["sample"], rfq["spec"]):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(p["bags"] * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        markup = min(max(p["markup"], MIN_MARKUP), MAX_MARKUP)
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * ctx.tv[lot["lot_id"]], 2)})
        seen.add(key)
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    """Returns (offers, source) where source is "llm" or "rules"."""
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    plan = llm_plan(state, ctx, cands) if ctx.llm else None
    if plan:
        # The LLM tends to offer too little. Rules fill whatever volume it left unused
        # (guard skips lot×RFQ pairs the LLM already used and stops at the round capacity).
        offers = guard(state, ctx, plan + rule_plan(state, ctx, cands))
        n_llm = len(guard(state, Context(ctx.rules, ctx.calendar, ctx.tv, ctx.rng), plan))
        return offers, "llm" if len(offers) == n_llm else f"llm+rules ({n_llm}+{len(offers) - n_llm})"
    return guard(state, ctx, rule_plan(state, ctx, cands)), "rules"
