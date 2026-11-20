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
import time
from dataclasses import dataclass, field

BAG = 60
# Markup per buyer type. The starter asks 1.00 × TV from everyone.
# TODO: buyer types do not pay the same (Specialty pays the most, Instant the least), and these never change.
#       Price each type differently and move the markups after each round's results
#       (ACCEPTED -> ask a bit more, OUTBID / ABOVE_LIMIT -> ask less) — จุดนี้ปรับแล้วได้ผลมากที่สุด
MARKUP = {"SPECIALTY": 1.00, "COMMERCIAL": 1.00, "INSTANT": 1.00}
MIN_MARKUP, MAX_MARKUP = 0.82, 1.30   # unsold beans go to salvage at 0.80 × TV, so never go near that
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
# [T26 20พย. R6+] จาก market_summary R5: Commercial มีคู่แข่ง 25–50 ราย/ใบ เฉลี่ย 111–114 บาท/กก. เราแพ้ 83%
# capacity 300 กก./รอบ แพ้ = เสียช่องทั้งรอบ -> จำกัดราคาสูงสุดต่อประเภทให้ชนะ (ขาย ×0.89 ยังดีกว่าเหลือ ×0.80)
PRICE_CAP = {"SPECIALTY": 0.98, "COMMERCIAL": 0.89, "INSTANT": 0.90}
# [T26 20พย. R13+] R12: Commercial เฉลี่ย ~101 บาท/กก. เราแพ้ 4/5 ที่ ×0.87–0.89 · Instant เราชนะ 100% ที่ ×0.88–0.90
# ช่วงท้ายให้ทุกล็อตยื่น Instant ได้และได้ช่องก่อน · ราคาต่ำสุดลดเป็น ×0.82 (ยังเหนือ ×0.80 ของที่ขายไม่ออก)
ENDGAME_INSTANT_RL = 3
INSTANT_FIRST = True                  # ล็อตที่ขายได้แค่ Instant (คู่แข่งน้อย ซื้อไม่ครบ) ได้ช่อง capacity ก่อน
CAP_PRESSURE = 0.02                   # capacity ไม่พอขายหมด -> ลดราคาเพิ่มเท่านี้
LADDER_ACROSS = False                 # ใบละ 1 กระสอบ + ยังไม่รู้จักผู้ซื้อ -> ไล่ขั้นบันไดข้ามใบ
SKIP_SPEC_FAILED = True               # เคยโดน SPEC_MISMATCH กับประเภทผู้ซื้อไหน -> เลิกยื่นคู่นั้น (และปล่อยไปขาย Instant ได้ทันที)
USE_CALENDAR = True                   # [T26] เปิด 19 พ.ย. (sim_day3: PR +0.0006 ไม่เคยแย่ลง) ใช้ demand_weight จากปฏิทินตลาด แบ่งขายมากในรอบที่ต้องการซื้อสูง
FESTIVAL_SPECIALTY_ONLY = True       # [T26] 20 พ.ย.: น้ำหนักเทศกาล (>1) ใช้กับล็อตเกรด Specialty เท่านั้น (False = ทุกล็อตเหมือนเดิม)
LLM_HISTORY = 3                       # ส่งผลย้อนหลังให้ LLM กี่รอบ (main.py ตั้งจาก .env LLM_HISTORY)
EXTRA_PROMPT = ""                     # คำสั่งเพิ่มเติมถึง LLM (main.py อ่านจาก llm_prompt_extra.txt)
LLM_MODE = "hybrid"                   # main.py ตั้งจาก .env: "hybrid" = กฎ+LLM ประสาน (กฎทำแผน LLM ปรับ ±COORD_MAX) | "llm" = LLM ล้วน
RFQ_BALANCE = True                    # กระจายล็อตไปคนละใบ RFQ (กันหลายล็อตรุมใบเดียวแล้วแย่งกันเอง)
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
    llm_info: dict = field(default_factory=dict)   # [T26] สิ่งที่ LLM เห็น/ตอบรอบนี้ (ไว้แสดงบน dashboard)


def spec_ok(sample: dict, spec: dict) -> bool:
    # TODO: the sample is noisy. A lot just inside the limit may really be outside -> SPEC_MISMATCH.
    #       ลองเว้น margin เล็กน้อยสำหรับล็อตที่ค่าใกล้ขอบสเปก
    return (sample["defect_pct"] <= spec["max_defect_pct"] and sample["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], ctx.rules["timeline"]["rounds"]
    return total if rnd["kind"] == "practice" else total - rnd["number"] + 1


def demand_weights(ctx: Context, total: int) -> dict[int, float]:
    """[T26] ปฏิทินตลาด -> น้ำหนักความต้องการซื้อของแต่ละรอบ (ไม่มีข้อมูล = 1.0 ทุกรอบ)"""
    w = {r: 1.0 for r in range(1, total + 1)}
    for band in ctx.calendar or []:
        try:
            a, _, b = str(band.get("rounds", "")).partition("-")
            lo, hi = int(a), int(b or a)
            dw = float(band.get("demand_weight") or 1.0)
            for r in range(lo, hi + 1):
                if r in w:
                    w[r] = max(dw, 0.05)
        except (TypeError, ValueError):
            continue
    return w


_SPEC_SEEN: dict = {}   # [T26] สเปก Specialty ที่เคยเห็นจาก RFQ (รอบที่ไม่มี RFQ Specialty ยังรู้ว่าล็อตไหนเข้าเกรด)


def specialty_lots(state: dict) -> set:
    """[T26] ล็อตที่ผลวัดผ่านสเปก Specialty (ผู้ซื้อกลุ่มที่เทศกาลดันความต้องการ)"""
    specs = [r["spec"] for r in state.get("rfqs", []) if r.get("buyer_type") == "SPECIALTY"]
    if specs:                                              # จำสเปกที่หลวมที่สุดที่เห็น
        _SPEC_SEEN.update(max_defect_pct=max(x["max_defect_pct"] for x in specs),
                          max_moisture_pct=max(x["max_moisture_pct"] for x in specs),
                          min_screen=min((x["min_screen"] for x in specs if x["min_screen"] is not None), default=None))
    if not _SPEC_SEEN:
        return set()
    return {lot["lot_id"] for lot in state["lots"] if spec_ok(lot["sample"], _SPEC_SEEN)}


def sell_budget(state: dict, ctx: Context) -> dict[str, int]:
    """How many kg of each lot to offer this round: spread sales over the rounds that are left."""
    # [T26] ใช้ปฏิทินตลาด: รอบที่ความต้องการซื้อสูง (เทศกาล) แบ่งขายมากกว่า รอบที่ต่ำแบ่งน้อยกว่า
    #       ปฏิทินเรียบ (ปกติทุกรอบ) = แบ่งเท่า ๆ กันเหมือนเดิม
    # [T26] 20 พ.ย.: เทศกาลดันความต้องการเฉพาะ Specialty -> น้ำหนัก >1 ใช้กับล็อตเข้าเกรด Specialty เท่านั้น
    #       ล็อตอื่นเจอแต่ช่วงใกล้ปิด (น้ำหนัก <1) ส่วนเทศกาลนับเป็น 1.0
    left = rounds_left(state, ctx)
    total = ctx.rules["timeline"]["rounds"]
    now = total - left + 1
    w = demand_weights(ctx, total) if USE_CALENDAR else {}
    w_flat = {r: min(v, 1.0) for r, v in w.items()} if FESTIVAL_SPECIALTY_ONLY else w
    spec_lots = specialty_lots(state) if (w and FESTIVAL_SPECIALTY_ONLY) else set()

    def frac_of(wt: dict) -> float:
        return (wt.get(now, 1.0) / sum(wt.get(r, 1.0) for r in range(now, total + 1))) if wt else 1 / max(left, 1)

    out = {}
    for lot in state["lots"]:
        frac = frac_of(w if (not FESTIVAL_SPECIALTY_ONLY or lot["lot_id"] in spec_lots) else w_flat)
        avail = lot["kg_available"] // BAG * BAG
        share = avail if left <= 1 else math.ceil(avail * frac * PACE / BAG) * BAG
        out[lot["lot_id"]] = min(avail, max(BAG, share)) if avail >= BAG else 0
    return out


def spec_failed(memory) -> set:
    """[T26] คู่ (ล็อต, ประเภทผู้ซื้อ) ที่เคยโดน SPEC_MISMATCH = คุณภาพจริงไม่ผ่าน (ค่าตัวอย่างหลอก) -> เลิกยื่นคู่นี้"""
    bad = set()
    if memory is None:
        return bad
    for rnd in memory.rounds:
        types = {s["rfq_id"]: s.get("buyer_type") for s in memory.sent.get(rnd["round_id"], [])}
        for r in rnd.get("my_offers", []):
            if r.get("status") == "SPEC_MISMATCH" and types.get(r["rfq_id"]):
                bad.add((r["lot_id"], types[r["rfq_id"]]))
    return bad


def candidates(state: dict, ctx: Context) -> list[dict]:
    budget = sell_budget(state, ctx)
    bad = spec_failed(ctx.memory) if SKIP_SPEC_FAILED else set()
    out = []
    for lot in state["lots"]:
        if budget[lot["lot_id"]] < BAG:
            continue
        for rfq in state["rfqs"]:
            if (lot["lot_id"], rfq["buyer_type"]) in bad:
                continue                               # เคยไม่ผ่านสเปกจริงกับผู้ซื้อประเภทนี้แล้ว
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
            if sold and len(lost) > len(sold):
                # [T26 20พย.] แพ้ราคามากกว่าขายได้ -> คู่แข่งต่ำกว่าเรา ลดลงจากขั้นต่ำสุดที่ยื่น (เดิมค้างราคาที่ขายได้ ทำให้ OUTBID 83%)
                k["best"] = max(sold + ([k["best"]] if k["best"] else []))
                k["streak"] += 1
                step = min(STEP_OUTBID * k["streak"], OUTBID_MAX_STEP) if OUTBID_ACCEL else STEP_OUTBID
                nxt = min(m for m, _ in obs) - step
            elif sold:
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
    pool = [c for c in cands if not (c["buyer_type"] == "INSTANT" and c["lot_id"] in premium_ok and rl > ENDGAME_INSTANT_RL)]
    price = {id(c): markup_for(c, learned, rl) for c in pool}
    # [T26] capacity ต่อรอบ (วันที่ 18, 20): ถ้าของที่ขายได้เหลือมากกว่า capacity ที่เหลือทั้งเกม -> ยอมลดราคา
    cap = state.get("capacity_left_kg")
    if cap is not None and CAP_PRESSURE:
        sellable = sum(l["kg_available"] for l in state["lots"] if l["lot_id"] in {c["lot_id"] for c in cands})
        if sellable > cap * rl:
            for k, (m, known, ceil) in price.items():
                price[k] = (max(m - CAP_PRESSURE, MIN_MARKUP), known, ceil)
    pool.sort(key=lambda c: (0 if INSTANT_FIRST and (c["lot_id"] not in premium_ok or (rl <= ENDGAME_INSTANT_RL and c["buyer_type"] == "INSTANT")) else 1,
                             -c["tv"], -price[id(c)][0]))   # sort แบบ stable: RFQ ที่ราคาเท่ากันยังคงลำดับสุ่ม
    plan = []
    # กระจาย: ล็อตหนึ่งยื่นได้หลาย RFQ ต่อรอบ (ใบละไม่เกิน budget / SPREAD) ถ้าแพ้ใบหนึ่งยังมีลุ้นอีกใบ
    per_rfq = {l: max(1, math.ceil(b / BAG / SPREAD)) for l, b in budget.items()}
    step_i: dict = {}                                       # ล็อต × ผู้ซื้อที่ยังไม่รู้จัก -> ใช้ขั้นบันไดถึงขั้นไหนแล้ว
    if RFQ_BALANCE:
        # [T26] กระจายล็อตไปคนละใบ: แต่ละล็อตเลือกผู้ซื้อที่จ่ายดีก่อน แล้วในผู้ซื้อเดียวกัน
        # เลือกใบที่เรายังยื่นน้อยก่อน (ไม่ให้หลายล็อตรุมใบเดียวแล้วแย่งช่องกันเอง)
        used: dict = {}
        by_lot: dict = {}
        for c in pool:
            by_lot.setdefault(c["lot_id"], []).append(c)
        ordered = []
        for lot_cs in by_lot.values():                       # ล็อตเรียง TV สูงก่อนตาม pool
            for c in sorted(lot_cs, key=lambda c: (-round(price[id(c)][0], 3), used.get(c["rfq_id"], 0))):
                ordered.append(c)
                if min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]], per_rfq[c["lot_id"]]) > 0:
                    used[c["rfq_id"]] = used.get(c["rfq_id"], 0) + 1
                    budget_sim = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]], per_rfq[c["lot_id"]])
                    budget[c["lot_id"]] -= budget_sim * BAG            # จองชั่วคราวเพื่อจัดลำดับ แล้วคืนด้านล่าง
                    rfq_left[c["rfq_id"]] -= budget_sim
        budget = sell_budget(state, ctx)                     # คืนค่าเดิม แล้ววนจริงตามลำดับใหม่
        rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
        pool = ordered
    for c in pool:
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]], per_rfq[c["lot_id"]])
        if bags <= 0:
            continue
        m, known, ceil = price[id(c)]
        if LADDER_ACROSS and not known and bags == 1:
            # ใบละ 1 กระสอบ ขั้นบันไดในใบเดียวทำไม่ได้ -> ไล่ขั้นข้ามใบแทน (ใบแรก m, ใบสอง m-0.03, ...)
            i = step_i.get((c["lot_id"], c["buyer_type"]), 0)
            step_i[(c["lot_id"], c["buyer_type"])] = i + 1
            m = max(m + LADDER[i % len(LADDER)], MIN_MARKUP)
        merged_cap: dict = {}
        for n, mk in split_bags(bags, m, known, ceil):
            mk = round(max(min(mk, PRICE_CAP.get(c["buyer_type"], 9.0)), MIN_MARKUP), 3)
            merged_cap[mk] = merged_cap.get(mk, 0) + n
        for mk, n in sorted(merged_cap.items(), reverse=True):
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
    if LLM_MODE == "llm":
        # [T26] LLM ล้วน: ไม่บอกคำตอบของกฎ ให้ LLM คิดเองจากผลที่ผ่านมา (เก็บค่าของกฎไว้เทียบบน dashboard เท่านั้น)
        hint = "Choose markups yourself from the recent results (ceilings are hidden; learn them)."
        _learned, _rl = learn(ctx.memory, ctx.tv), rounds_left(state, ctx)
        hidden_rule = [round(markup_for(c, _learned, _rl)[0], 3) for c in cands]
    else:
        # [T26] กฎ + LLM: แนบตัวคูณที่กฎ T26 คำนวณจากผลจริงของผู้ซื้อแต่ละราย ให้ LLM เห็นด้วยหรือปรับพร้อมเหตุผล
        learned, rl = learn(ctx.memory, ctx.tv), rounds_left(state, ctx)
        for row, c in zip(table, cands):
            row["rule_markup"] = round(markup_for(c, learned, rl)[0], 3)
        hidden_rule = None
        hint = ("Each candidate has rule_markup: what our tested rules suggest for that buyer, learned from real results "
                "(raise after sales, lower after OUTBID/ABOVE_LIMIT). Use it unless you have a clear reason; "
                "never go above a markup that buyer rejected as ABOVE_LIMIT.")
    user = "\n".join([
        f"Round {rnd['round_id']} ({rnd['kind']}), market calendar: {rnd['calendar']}. Rounds left: {rounds_left(state, ctx)}.",
        f"You may offer at most {state['capacity_left_kg'] // BAG if state['capacity_left_kg'] is not None else 'any'} bags in total this round.",
        f"Plan to offer about {target} bags in total this round: unsold beans lose 20% of their value at the end.",
        hint,
        "Recent results:", ctx.memory.summary(last=LLM_HISTORY) if ctx.memory else "none",
        "Candidates (lot x RFQ, already spec-checked):", json.dumps(table, ensure_ascii=False),
        "Pick offers. Use each candidate at most once; bags <= max_bags.",
    ])
    # TODO: give the LLM more to reason about — buyer personas' habits, the calendar ahead, price trends
    system = SYSTEM + (("\n\nExtra instructions from the team:\n" + EXTRA_PROMPT) if EXTRA_PROMPT.strip() else "")
    t_ask = time.time()
    answer = ctx.llm.ask_json(system, user, SCHEMA, timeout=time_for_llm)
    ctx.llm_info = {"mode": LLM_MODE, "seconds": round(time.time() - t_ask, 2), "timeout": round(time_for_llm, 1),
                    "system": system, "user": user, "raw": getattr(ctx.llm, "last_text", None),
                    "usage": getattr(ctx.llm, "last_usage", None), "error": ctx.llm.last_error,
                    "n_candidates": len(cands), "target_bags": target, "ok": False, "offers": []}
    if answer is None or not isinstance(answer.get("offers"), list):
        ctx.notes.append(f"llm failed: {ctx.llm.last_error}")
        return None
    ctx.llm_info.update(ok=True, reason=str(answer.get("reason"))[:300])
    plan = []
    for o in answer["offers"]:
        try:
            c = cands[int(o["candidate"])]
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": int(o["bags"]), "markup": float(o["markup"])})
            ctx.llm_info["offers"].append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "buyer_id": c["buyer_id"],
                                          "buyer_type": c["buyer_type"], "bags": int(o["bags"]), "markup": float(o["markup"]),
                                          "rule_markup": table[int(o["candidate"])].get("rule_markup")
                                          or (hidden_rule[int(o["candidate"])] if hidden_rule else None)})
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


# ===================== [T26] กฎ + LLM แบบประสาน: กฎคุมโครง · LLM ปรับละเอียด =====================
COORD_MAX = 0.02                      # LLM ปรับตัวคูณของผู้ซื้อแต่ละรายได้ไม่เกิน ±เท่านี้จากที่กฎเสนอ
COORD_DRIFT = 0.02                    # กดลงได้ไม่ต่ำกว่า (ราคาสูงสุดที่ผู้ซื้อรายนั้นเคยจ่าย − เท่านี้) · None = ไม่กัน

COORD_SYSTEM = """You are the pricing advisor of a coffee farm's sales agent in a sealed-offer market.
Tested rules already built this round's plan: which lot goes to which buyer, how many 60 kg bags, and a
markup per buyer (price = markup x true value). Your job is ONLY to fine-tune each buyer's markup by a
delta between -{m} and +{m} (0 = keep the rule). You cannot change lots, buyers or bags.

How buyers decide: they drop offers above their hidden ceiling (ABOVE_LIMIT), sort the rest by
price/value (cheapest first) and buy until full; the rest are OUTBID. Unsold beans are worth only 0.80 at the end.
Use what the rules do not look at:
- market: if a buyer bought less than it wanted last round, competition there is weak -> you may raise a little.
- market: many offers and a full buy -> strong competition -> lower a little if we lost there.
- our results per buyer: repeated OUTBID -> lower; ABOVE_LIMIT -> never go up for that buyer.
- few rounds left with many bags left -> lower a little to sell; nothing left to sell -> no change needed.
Small or zero deltas are usually right. Answer only JSON matching the schema; keep reason short."""

COORD_SCHEMA = {"type": "object", "required": ["adjust", "reason"], "properties": {
    "adjust": {"type": "array", "items": {"type": "object", "required": ["buyer_id", "delta"], "properties": {
        "buyer_id": {"type": "string"}, "delta": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 300}}}


def _buyer_history(memory, last: int) -> dict:
    """ผลของเราต่อผู้ซื้อแต่ละรายใน n รอบล่าสุด: buyer_id -> ["R3 x1.00 ACCEPTED", ...]"""
    out: dict = {}
    if memory is None:
        return out
    for rnd in memory.rounds[-last:]:
        sent = {(s["rfq_id"], s["lot_id"]): s for s in memory.sent.get(rnd["round_id"], [])}
        for r in rnd.get("my_offers", []):
            s = sent.get((r["rfq_id"], r["lot_id"]))
            if s and s.get("buyer_id"):
                out.setdefault(s["buyer_id"], []).append(f"{rnd['round_id']} x{s.get('markup', '?')} {r['status']}")
    return out


def _market_by_buyer(state: dict, memory) -> dict:
    """ภาพรวมตลาดรอบล่าสุดต่อผู้ซื้อ (ทุกทีม): ต้องการ / ซื้อได้ / กี่ซอง / ราคาเฉลี่ย"""
    if memory is None or not memory.rounds:
        return {}
    name2id = {r["buyer"]: r.get("buyer_id") or r["buyer"] for r in state.get("rfqs", [])}
    out: dict = {}
    for m in memory.rounds[-1].get("market_summary", []):
        bid = name2id.get(m["buyer"], m["buyer"])
        a = out.setdefault(bid, {"wanted_bags": 0, "bought_bags": 0, "offers": 0, "avg_prices": []})
        a["wanted_bags"] += m["wanted_kg"] // BAG
        a["bought_bags"] += m["bought_kg"] // BAG
        a["offers"] += m["offers"]
        if m.get("price_avg") is not None:
            a["avg_prices"].append(m["price_avg"])
    return out


def llm_coordinate(state: dict, ctx: Context, plan: list[dict]) -> tuple[list[dict], int]:
    """ให้ LLM ปรับตัวคูณรายผู้ซื้อจากแผนของกฎ (±COORD_MAX) · ล้ม/ไม่ทัน -> คืนแผนของกฎเดิม"""
    rfq_b = {r["rfq_id"]: (r.get("buyer_id") or r["buyer"], r["buyer_type"]) for r in state["rfqs"]}
    learned = learn(ctx.memory, ctx.tv)
    buyers: dict = {}
    for p in plan:
        bid, typ = rfq_b[p["rfq_id"]]
        b = buyers.setdefault(bid, {"buyer_id": bid, "type": typ, "rule_markups": set(), "bags": 0})
        b["rule_markups"].add(round(p["markup"], 3))
        b["bags"] += p["bags"]
    if not buyers:
        return plan, 0
    time_for_llm = ctx.seconds_left - 5
    if time_for_llm < 3:
        ctx.notes.append("no time for the LLM this round")
        return plan, 0
    hist, mkt = _buyer_history(ctx.memory, LLM_HISTORY), _market_by_buyer(state, ctx.memory)
    rows = []
    for bid, b in buyers.items():
        k = learned.get(bid) or {}
        rows.append({"buyer_id": bid, "type": b["type"], "bags_planned": b["bags"],
                     "rule_markups": sorted(b["rule_markups"]),
                     "known_ceiling": k.get("ceil") if k.get("ceil", 9) < 9 else None,
                     "best_sold": round(k["best"], 3) if k.get("best") else None,
                     "our_recent": hist.get(bid, []),
                     "market_last_round": {kk: (round(sum(v) / len(v), 1) if kk == "avg_prices" and v else v)
                                           for kk, v in mkt.get(bid, {}).items()} or None})
    bags_left = sum(l["kg_available"] for l in state["lots"]) // BAG
    user = "\n".join([
        f"Round {state['round']['round_id']}, rounds left: {rounds_left(state, ctx)}, our sellable bags left: {bags_left}.",
        f"Rule plan per buyer (fine-tune each with a delta in [-{COORD_MAX:.2f}, {COORD_MAX:.2f}]):",
        json.dumps(rows, ensure_ascii=False, default=list)])
    system = COORD_SYSTEM.replace("{m}", f"{COORD_MAX:.2f}") + (("\n\nExtra instructions from the team:\n" + EXTRA_PROMPT) if EXTRA_PROMPT.strip() else "")
    t_ask = time.time()
    answer = ctx.llm.ask_json(system, user, COORD_SCHEMA, timeout=time_for_llm)
    ctx.llm_info = {"mode": "hybrid", "seconds": round(time.time() - t_ask, 2), "timeout": round(time_for_llm, 1),
                    "system": system, "user": user, "raw": getattr(ctx.llm, "last_text", None),
                    "usage": getattr(ctx.llm, "last_usage", None), "error": ctx.llm.last_error,
                    "n_candidates": len(buyers), "target_bags": sum(b["bags"] for b in buyers.values()),
                    "ok": False, "offers": []}
    if answer is None or not isinstance(answer.get("adjust"), list):
        ctx.notes.append(f"llm failed: {ctx.llm.last_error}")
        return plan, 0
    ctx.llm_info.update(ok=True, reason=str(answer.get("reason"))[:300])
    delta: dict = {}
    for a in answer["adjust"]:
        try:
            if str(a["buyer_id"]) in buyers:
                delta[str(a["buyer_id"])] = max(-COORD_MAX, min(COORD_MAX, float(a["delta"])))
        except (KeyError, TypeError, ValueError):
            ctx.notes.append(f"llm bad adjust ignored: {a}")
    out, changed = [], 0
    for p in plan:
        bid, typ = rfq_b[p["rfq_id"]]
        d = delta.get(bid, 0.0)
        ceil = (learned.get(bid) or {}).get("ceil", 9.0)
        m = p["markup"] + d
        if d > 0:
            m = min(m, ceil - 0.01)                      # ห้ามดันขึ้นชนเพดานที่รู้แล้ว
        best = (learned.get(bid) or {}).get("best")
        if d < 0 and best and COORD_DRIFT is not None:
            # กันราคาไหลลงสะสม: กฎเรียนรู้จากราคาที่ยื่นจริง ถ้า LLM กดลงทุกรอบ ราคาจะลดต่อเนื่อง
            # -> ไม่ให้ต่ำกว่าราคาที่ผู้ซื้อรายนี้เคยจ่ายสูงสุด − COORD_DRIFT
            m = max(m, min(p["markup"], best - COORD_DRIFT))
        m = min(m, PRICE_CAP.get(typ, 9.0))              # [T26 20พย.] LLM ดันเกินเพดานราคาต่อประเภทไม่ได้
        m = round(min(max(m, MIN_MARKUP), MAX_MARKUP), 3)
        changed += abs(m - p["markup"]) > 1e-9
        out.append({**p, "markup": m})
        ctx.llm_info["offers"].append({"lot_id": p["lot_id"], "rfq_id": p["rfq_id"], "buyer_id": bid, "buyer_type": typ,
                                       "bags": p["bags"], "markup": m, "rule_markup": p["markup"]})
    ctx.notes.append(f"llm reason: {str(answer.get('reason'))[:200]}")
    return out, changed


def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    """Returns (offers, source) where source is "llm" or "rules"."""
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    if ctx.llm and LLM_MODE == "hybrid":
        # [T26] กฎ + LLM แบบประสาน: กฎทำแผนทั้งหมด แล้ว LLM ปรับตัวคูณรายผู้ซื้อ ±0.03
        plan = rule_plan(state, ctx, cands)
        n_before = len(ctx.notes)
        adjusted, changed = llm_coordinate(state, ctx, plan)
        if any(n.startswith("llm failed") or n.startswith("no time") for n in ctx.notes[n_before:]):
            return guard(state, ctx, plan), "rules (LLM ไม่ตอบ)"
        return guard(state, ctx, adjusted), f"rules+llm (ปรับ {changed} ซอง)"
    plan = llm_plan(state, ctx, cands) if ctx.llm else None
    if plan and LLM_MODE == "llm":
        # [T26] LLM ล้วน: ใช้แผนของ LLM อย่างเดียว ไม่เติมด้วยกฎ (guard ยังตรวจทุกซองเพื่อให้ถูกกติกา)
        return guard(state, ctx, plan), "llm-only"
    if plan:
        # The LLM tends to offer too little. Rules fill whatever volume it left unused
        # (guard skips lot×RFQ pairs the LLM already used and stops at the round capacity).
        offers = guard(state, ctx, plan + rule_plan(state, ctx, cands))
        n_llm = len(guard(state, Context(ctx.rules, ctx.calendar, ctx.tv, ctx.rng), plan))
        return offers, "llm" if len(offers) == n_llm else f"llm+rules ({n_llm}+{len(offers) - n_llm})"
    return guard(state, ctx, rule_plan(state, ctx, cands)), "rules" if not ctx.llm else "rules (LLM ไม่ตอบ)"
