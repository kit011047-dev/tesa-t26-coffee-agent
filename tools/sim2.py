"""[T26] sim2.py: ตลาดจำลองหลายร้อยเกม ไว้เทียบกลยุทธ์ (ไม่แตะตลาดจริง)

ปรับค่าจากข้อมูล sandbox 17 พ.ย. (5 เกม):
  - Commercial: เพดานลับต่อผู้ซื้อ ~1.00–1.07 × TV, คู่แข่งยื่นราว 0.96–1.03 (ต่างกันทุกเกม)
  - Instant   : เพดาน ~0.90–0.97, คู่แข่งราว 0.86–0.94
  - ล็อต: บางล็อต REJECT, บางล็อตขายได้แค่ Instant (ความชื้น/เมล็ดเสียเกินเกณฑ์ Commercial)
ผู้ซื้อเรียงซองจาก ราคา÷TV ถูกสุดก่อน, ตัดซองที่เกินเพดาน, จ่ายตามราคาที่ยื่น

    python tools/sim2.py              # เทียบทุกแบบที่ตั้งไว้ใน VARIANTS
"""
import importlib
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import strategy  # noqa: E402

BAG = 60
C_SPEC = dict(max_defect_pct=7.0, max_moisture_pct=12.5, min_screen=None)
I_SPEC = dict(max_defect_pct=15.0, max_moisture_pct=14.0, min_screen=None)
BUYERS = [("B3", "COMMERCIAL"), ("B4", "COMMERCIAL"), ("B5", "INSTANT"), ("B6", "INSTANT")]


class Mem:
    def __init__(self):
        self.sent, self.rounds = {}, []


def make_game(rng: random.Random, harsh: float | None = None):
    lots = []
    for i in range(4):
        u = rng.random()
        if u < 0.55:
            s = dict(defect_pct=rng.uniform(0.5, 6.8), moisture_pct=rng.uniform(10.5, 12.4), screen=15); tv = rng.uniform(105, 133)
        elif u < 0.8:
            s = dict(defect_pct=rng.uniform(1, 6), moisture_pct=rng.uniform(12.6, 13.9), screen=15); tv = rng.uniform(95, 128)
        else:
            s = dict(defect_pct=rng.uniform(16, 25), moisture_pct=rng.uniform(10, 13), screen=14); tv = rng.uniform(35, 60)
        lots.append((f"L{i+1}", rng.randint(2, 10) * BAG, s, round(tv, 2)))
    h = rng.random() if harsh is None else harsh          # 0 = คู่แข่งใจดี, 1 = ตัดราคาแรง
    ceil = {b: (rng.uniform(1.00, 1.07) if t == "COMMERCIAL" else rng.uniform(0.90, 0.97)) for b, t in BUYERS}
    mu = {b: (1.03 - 0.07 * h if t == "COMMERCIAL" else 0.94 - 0.08 * h) + rng.gauss(0, 0.01) for b, t in BUYERS}
    return lots, ceil, mu


NOISE = 0.0  # ค่าจริงต่างจากค่าตัวอย่าง (sd ของ % เมล็ดเสีย และ % ความชื้น) ผู้ซื้อตรวจสเปกกับค่าจริง
CAL = None   # น้ำหนักความต้องการซื้อต่อรอบ เช่น [1,1,1,1.6,1.6,1] (None = ปกติทุกรอบ)
ROUNDS = 6   # จำนวนรอบ (วันที่ 18: 8 รอบ)
LLM_OBJ = None  # ใส่ LLM ปลอมเพื่อทดสอบโหมด กฎ + LLM
CAP = None   # None = ไม่จำกัด, ตัวเลข = capacity ต่อรอบ (เท่าของส่วนแบ่งเฉลี่ยต่อรอบ) เช่น 1.4
VIOLATIONS = []


def play(seed: int, harsh: float | None = None) -> float:
    rng = random.Random(seed)
    lots, ceil, mu = make_game(rng, harsh)
    tv = {l: v for l, _, _, v in lots}
    true = {l: dict(s, defect_pct=s['defect_pct'] + rng.gauss(0, NOISE), moisture_pct=s['moisture_pct'] + rng.gauss(0, NOISE * 0.5))
            for l, _, s, _ in lots}
    cal = [{"rounds": f"{i+1}-{i+1}", "label": "x", "demand_weight": w} for i, w in enumerate(CAL)] if CAL else []
    left = {l: kg for l, kg, _, _ in lots}
    mem, rev = Mem(), 0.0
    rules = {"timeline": {"rounds": ROUNDS}}
    total_bags = sum(kg for _, kg, _, _ in lots) // BAG
    cap_kg = None if CAP is None else math.ceil(total_bags / ROUNDS * CAP) * BAG
    for rn in range(1, ROUNDS + 1):
        rfqs = []
        k = 0
        for b, t in BUYERS:
            for _ in range(2):
                k += 1
                bags = max(1, round(rng.randint(4, 15) * (CAL[rn - 1] if CAL else 1)))
                rfqs.append(dict(rfq_id=f"R{rn}-{k:02d}", buyer=b, buyer_id=b, buyer_type=t, kg=bags * BAG, bags=bags,
                                 spec=C_SPEC if t == "COMMERCIAL" else I_SPEC))
        state = dict(round=dict(round_id=f"R{rn}", kind="real", number=rn, of=ROUNDS, calendar="ปกติ"), rfqs=rfqs,
                     lots=[dict(lot_id=l, kg_available=left[l], kg_remaining=left[l], sample=s) for l, _, s, _ in lots],
                     capacity_left_kg=cap_kg)
        ctx = strategy.Context(rules, cal, tv, random.Random(seed * 7 + rn), mem, LLM_OBJ, 60)
        offers, _ = strategy.decide(state, ctx)
        rmap = {r["rfq_id"]: r for r in rfqs}
        if cap_kg is not None and sum(o["kg"] for o in offers) > cap_kg:
            VIOLATIONS.append((seed, rn, sum(o["kg"] for o in offers), cap_kg))
        used = {}
        for o in offers:
            used[o["lot_id"]] = used.get(o["lot_id"], 0) + o["kg"]
        for l, kg in used.items():
            if kg > left[l]:
                VIOLATIONS.append((seed, rn, l, kg, left[l]))
        mem.sent[f"R{rn}"] = [{**o, "buyer_type": rmap[o["rfq_id"]]["buyer_type"], "buyer_id": rmap[o["rfq_id"]]["buyer_id"]}
                              for o in offers]
        res = []
        for r in rfqs:
            b = r["buyer_id"]
            # คู่แข่ง: กระสอบละซอง ตัวคูณ ~ N(ระดับของรอบนี้, 0.02)
            level = mu[b] + rng.gauss(0, 0.012)
            base_bags = r["bags"] / (CAL[rn - 1] if CAL else 1)          # คู่แข่งไม่ตามปฏิทิน: ยื่นเท่าเดิม
            comp = [rng.gauss(level, 0.02) for _ in range(int(base_bags * rng.uniform(0.5, 1.4)))]
            book = [(m, None) for m in comp if m <= ceil[b]]
            mine = [o for o in offers if o["rfq_id"] == r["rfq_id"]]
            for o in mine:
                m = o["price_per_kg"] / tv[o["lot_id"]]
                if m <= ceil[b] and strategy.spec_ok(true[o["lot_id"]], r["spec"]):
                    book += [(m, o)] * (o["kg"] // BAG)
            book.sort(key=lambda x: x[0])
            filled = {}
            for m, o in book[:r["bags"]]:
                if o is not None:
                    filled[id(o)] = filled.get(id(o), 0) + 1
            for o in mine:
                m = o["price_per_kg"] / tv[o["lot_id"]]
                f = filled.get(id(o), 0)
                st = "SPEC_MISMATCH" if not strategy.spec_ok(true[o["lot_id"]], r["spec"]) else "ABOVE_LIMIT" if m > ceil[b] else ("OUTBID" if f == 0 else ("ACCEPTED" if f * BAG == o["kg"] else "PARTIAL"))
                left[o["lot_id"]] -= f * BAG
                rev += f * BAG * o["price_per_kg"]
                res.append(dict(rfq_id=o["rfq_id"], lot_id=o["lot_id"], kg=o["kg"], price_per_kg=o["price_per_kg"],
                                status=st, filled_kg=f * BAG))
        mem.rounds.append(dict(round_id=f"R{rn}", my_offers=res, market_summary=[]))
    total = sum(kg * v for _, kg, _, v in lots)
    return (rev + sum(0.8 * tv[l] * kg for l, kg in left.items())) / total


def evaluate(cfg: dict, seeds=range(300), harsh=None, cap=None, noise=0.0, cal=None, rounds=6) -> float:
    global CAP, NOISE, CAL, ROUNDS
    CAP, NOISE, CAL, ROUNDS = cap, noise, cal, rounds
    importlib.reload(strategy)
    for k, v in cfg.items():
        setattr(strategy, k, v)
    return statistics.mean(play(s, harsh) for s in seeds)


def baseline_plan(state, ctx, cands):
    budget = strategy.sell_budget(state, ctx)
    rl = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    plan = []
    for c in sorted(cands, key=lambda c: -c["tv"]):
        b = min(budget[c["lot_id"]] // BAG, rl[c["rfq_id"]])
        if b > 0:
            plan.append(dict(lot_id=c["lot_id"], rfq_id=c["rfq_id"], bags=b, markup=1.0))
            budget[c["lot_id"]] -= b * BAG
            rl[c["rfq_id"]] -= b
    return plan


VARIANTS = {
    "ตัวตั้งต้น (1.00 ตายตัว)": {"rule_plan": baseline_plan},
    "ปัจจุบัน": {},
    "+ถอยเร่ง": {"OUTBID_ACCEL": True},
    "+กระจาย 2 ใบ": {"SPREAD": 2},
    "+ถอยเร่ง +กระจาย": {"OUTBID_ACCEL": True, "SPREAD": 2},
}

if __name__ == "__main__":
    for name, cfg in VARIANTS.items():
        a, e, h = evaluate(cfg), evaluate(cfg, range(150), 0.1), evaluate(cfg, range(150), 0.9)
        print(f"{name:28s} PR เฉลี่ย {a:.4f} | คู่แข่งใจดี {e:.4f} | คู่แข่งตัดราคาแรง {h:.4f}")
