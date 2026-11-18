"""[T26] llm_try.py: ทดลอง LLM gateway ของผู้จัด แบบแห้ง (ไม่ยื่นอะไรเข้าตลาด)

ใช้ค่าใน .env: LLM_BASE_URL (ที่อยู่ gateway ที่ทีมงานแจ้ง), LLM_MODEL (event-llm), LLM_API_KEY (ว่าง = ใช้ TEAM_KEY)

    .venv/bin/python tools/llm_try.py            # 1) ต่อได้ไหม 2) ตอบ JSON ได้ไหม 3) เทียบ LLM กับกฎ บนสถานะตลาดตอนนี้
    .venv/bin/python tools/llm_try.py --rules    # ให้ LLM เห็นกฎของทีมใน prompt ด้วย (แบบ C ในเอกสาร)

ไม่แก้ .env และไม่เปิด USE_LLM · สิ่งที่ทำกับตลาดมีแค่ GET /v1/state และ /v1/calendar
"""
import glob
import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

import httpx  # noqa: E402

import strategy  # noqa: E402
from appraisal import estimate_lots  # noqa: E402
from llm import LLM  # noqa: E402
from memory import Memory  # noqa: E402

RULES_PROMPT = """
Follow these team rules unless recent results clearly say otherwise:
1. Starting markup for an unknown buyer: SPECIALTY 1.05, COMMERCIAL 1.00, INSTANT 0.92.
2. Per buyer from last round: all sold -> +0.01; some sold & higher lost -> keep; all OUTBID -> -0.01
   (more if it keeps losing); all ABOVE_LIMIT -> -0.03. Never go at/above a markup that was ABOVE_LIMIT.
3. Lots that fit COMMERCIAL/SPECIALTY: do not sell to INSTANT until 2 rounds are left.
4. Spread one lot over up to 3 RFQs. Offer ~1.6 x (bags left / rounds left). Last round: everything, markup -0.04.
5. Never below 0.85."""


def load_env() -> dict:
    env = {}
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> int:
    env = load_env()
    base, model = env.get("LLM_BASE_URL", ""), env.get("LLM_MODEL", "event-llm")
    key = env.get("LLM_API_KEY", "")
    if not key or "change-me" in key:                       # gateway ใช้ key เดียวกับตลาด
        key = env.get("TEAM_KEY", "")
    strategy.LLM_MODE = env.get("LLM_MODE", "hybrid") if "--rules" in sys.argv or env.get("LLM_MODE") != "llm" else "llm"
    if not base or "GATEWAY-ADDRESS" in base:
        print("❌ ยังไม่ได้ใส่ที่อยู่ gateway: แก้ LLM_BASE_URL ใน .env เป็นที่อยู่ที่ทีมงานแจ้ง (เช่น http://x.x.x.x:4000/v1)")
        return 1

    # 1) ต่อได้ไหม
    print(f"=== 1) ต่อ gateway: {base}  model={model}")
    try:
        r = httpx.get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        print(f"   HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:                                  # noqa: BLE001
        print(f"   ❌ ต่อไม่ได้: {e!r}")
        return 1

    # 2) ตอบ JSON ได้ไหม + ใช้เวลาเท่าไร
    llm = LLM(base, model, key, timeout=float(env.get("LLM_TIMEOUT", "60")), thinking=env.get("LLM_THINKING") == "1",
              max_tokens=int(env.get("LLM_MAX_TOKENS", "1500")), temperature=float(env.get("LLM_TEMPERATURE", "0.7")))
    strategy.LLM_HISTORY = int(env.get("LLM_HISTORY", "3"))
    extra = HERE / "llm_prompt_extra.txt"
    strategy.EXTRA_PROMPT = extra.read_text(encoding="utf-8") if extra.exists() else ""
    print(f"   ค่าที่ใช้: temperature {llm.temperature} · max_tokens {llm.max_tokens} · thinking {llm.thinking}"
          f" · ผลย้อนหลัง {strategy.LLM_HISTORY} รอบ · คำสั่งเพิ่มเติม {'มี' if strategy.EXTRA_PROMPT.strip() else 'ไม่มี'}")
    schema = {"type": "object", "required": ["markup", "reason"],
              "properties": {"markup": {"type": "number"}, "reason": {"type": "string", "maxLength": 200}}}
    print("=== 2) ทดสอบคำถามสั้น (ตอบเป็น JSON)")
    t0 = time.time()
    ans = llm.ask_json("Answer only JSON matching the schema.",
                       "Last round a COMMERCIAL buyer accepted our offer at markup 1.00 and rejected 1.03 as ABOVE_LIMIT. "
                       "What markup should we offer this round, and why?", schema, timeout=60)
    print(f"   {time.time() - t0:.1f} วินาที -> {ans if ans is not None else '❌ ' + str(llm.last_error)}")

    # 3) เทียบ LLM กับกฎ บนสถานะตลาดตอนนี้ (ไม่ส่ง)
    mk = {"Authorization": f"Bearer {env['TEAM_KEY']}"}
    st = httpx.get(env["MARKET_URL"].rstrip("/") + "/v1/state", headers=mk, timeout=10).json()
    cal = httpx.get(env["MARKET_URL"].rstrip("/") + "/v1/calendar", headers=mk, timeout=10).json()
    print(f"=== 3) ตลาดตอนนี้: {st.get('market')} {st.get('phase')} {(st.get('round') or {}).get('round_id')}")
    if st.get("phase") != "ROUND_OPEN" or not st.get("rfqs"):
        print("   ยังไม่อยู่ในรอบเปิด (ไม่มี RFQ) ให้รันใหม่ตอนรอบเปิดเพื่อเทียบการตัดสินใจ")
        return 0
    tv = estimate_lots(cal["rules"], st["lots"])
    files = sorted(glob.glob("logs/memory-*.json"), key=os.path.getmtime)
    mem = Memory(files[-1]) if files else None
    if mem is not None:
        mem.save = lambda: None                              # อ่านอย่างเดียว
    if "--rules" in sys.argv:
        strategy.SYSTEM = strategy.SYSTEM + RULES_PROMPT
    ctx = strategy.Context(cal["rules"], cal["calendar"], tv, random.Random(1), mem, llm, st.get("seconds_left") or 90)
    cands = strategy.candidates(st, ctx)
    t0 = time.time()
    lp = strategy.llm_plan(st, ctx, cands)
    sec = time.time() - t0
    rp = strategy.rule_plan(st, strategy.Context(cal["rules"], cal["calendar"], tv, random.Random(1), mem), cands)
    show = lambda plan: [f"{p['lot_id'][-1]}→{p['rfq_id']} {p['bags']}กส. x{p['markup']:.3f}" for p in plan]  # noqa: E731
    print(f"   LLM ({sec:.1f} วินาที, token {ctx.llm_info.get('usage')}): {show(lp) if lp else '❌ ไม่ได้คำตอบ -> agent จะใช้กฎแทน'}")
    for o in ctx.llm_info.get("offers", []):
        print(f"     {o['lot_id'][-1]}→{o['buyer_id']} LLM ×{o['markup']:.3f} กฎ ×{o['rule_markup'] or 0:.3f}")
    for n in ctx.notes:
        if n.startswith("llm"):
            print(f"   {n}")
    print(f"   กฎ T26:            {show(rp)}")
    print("   (ไม่ได้ส่งอะไรเข้าตลาด)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
