"""[T26] sim_day3.py: เทียบชุดราคาภายใต้คะแนน 2 แบบ สำหรับด่าน 3 (15 รอบ + ปฏิทิน + capacity)

  PR     = (รายได้ + 0.8 × TV ที่เหลือ) ÷ TV ทั้งหมด            (สูตรในสไลด์วันที่ 20)
  ratio  = รายได้ ÷ TV ของส่วนที่ขายได้                        (ตามที่ตอบใน Discord: ไม่คิดส่วนที่ขายไม่ได้)

    python tools/sim_day3.py
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim2  # noqa: E402

# ปฏิทินด่าน 3: R1–5 ปกติ · R6–8 เทศกาล · R9–12 ปกติ · R13–14 ลดลง · R15 ต่ำสุด (น้ำหนักความต้องการเป็นค่าสมมติ)
CAL = [1.0] * 5 + [1.4] * 3 + [1.0] * 4 + [0.7] * 2 + [0.5]
SEEDS = range(int(os.environ.get("SEEDS", "200")))

VARIANTS = {
    "ปัจจุบัน (ขั้นต่ำ 0.85)": {},
    "+ปฏิทิน": {"USE_CALENDAR": True},
    "ขั้นต่ำ 0.92": {"MIN_MARKUP": 0.92},
    "ขั้นต่ำ 0.95 Instant เริ่ม 0.97": {"MIN_MARKUP": 0.95, "START": {"SPECIALTY": 1.05, "COMMERCIAL": 1.00, "INSTANT": 0.97}},
    "ขั้นต่ำ 0.98 ไม่ลดท้ายเกม": {"MIN_MARKUP": 0.98, "ENDGAME": {}, "START": {"SPECIALTY": 1.05, "COMMERCIAL": 1.02, "INSTANT": 0.99}},
}


def run(cfg, harsh=None):
    pr, ratio, sold = [], [], []
    sim2.CAP, sim2.NOISE, sim2.CAL, sim2.ROUNDS = 1.6, 0.3, CAL, 15
    import importlib
    importlib.reload(sim2.strategy)
    for k, v in cfg.items():
        setattr(sim2.strategy, k, v)
    for s in SEEDS:
        pr.append(sim2.play(s, harsh))
        L = sim2.LAST
        ratio.append(L["rev"] / L["sold_tv"] if L["sold_tv"] else 0)
        sold.append(L["sold_tv"] / L["total_tv"])
    m = statistics.mean
    return m(pr), m(ratio), m(sold)


if __name__ == "__main__":
    print(f"{'ชุดราคา':34s} {'PR':>7s} {'ราคา÷TV':>8s} {'ขายได้':>7s} | คู่แข่งตัดราคาแรง: PR / ราคา÷TV")
    for name, cfg in VARIANTS.items():
        a = run(cfg)
        h = run(cfg, 0.9)
        print(f"{name:34s} {a[0]:7.4f} {a[1]:8.4f} {a[2]:7.1%} | {h[0]:.4f} / {h[1]:.4f}")
