"""Coffee market starter agent: poll the market, appraise once, send offers every round.

    python main.py              # play until the market closes
    python main.py --forever    # sandbox: keep playing game after game
"""
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import dashboard_bridge as dash                    # [T26] ส่งความคิดของ agent ไป dashboard 7 ขั้น
import pi_view                                     # [T26] ค่าจาก Pi (T1+T2) แสดงบน dashboard อย่างเดียว
import strategy
from appraisal import appraisal_items, estimate_lots
from llm import LLM
from market import Market, MarketError
from memory import Memory

HERE = Path(__file__).parent
LOGS = HERE / "logs"


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(event: str, **data) -> None:
    LOGS.mkdir(exist_ok=True)
    row = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    with open(LOGS / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    short = {k: v for k, v in data.items() if k not in ("offers", "notes")}
    print(row["time"][11:], event, json.dumps(short, ensure_ascii=False)[:160], flush=True)


def send_offers(api: Market, offers: list[dict]) -> list[dict]:
    """Send all offers at once; if the batch is rejected, send them one by one and skip the bad ones."""
    try:
        return api.offer_batch(offers)["offers"]
    except MarketError as e:
        log("batch_rejected", error=str(e))
    sent = []
    for o in offers:
        try:
            sent.append(api.offer(o))
        except MarketError as e:
            log("offer_rejected", offer=o, error=str(e))
    return sent


def llm_url_problem(url: str, team_key: str) -> str | None:
    """[T26] ที่อยู่ gateway ใช้ไม่ได้แน่ๆ -> บอกเหตุผล (None = ดูใช้ได้)"""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not url or "GATEWAY-ADDRESS" in url or not host:
        return "ยังไม่ได้ใส่ที่อยู่ gateway"
    if host.startswith("sk-") or (team_key and team_key.lower()[-8:] in url.lower()):
        return "ใส่ key ในช่องที่อยู่ gateway"
    if host in ("localhost", "127.0.0.1"):
        return "ที่อยู่เป็นเครื่องตัวเอง (localhost) ไม่ใช่ gateway ของงาน"
    return None


# [T26] โหมดกฎ + LLM: LLM ไม่ตอบติดกัน LLM_FAIL_LIMIT รอบ -> พัก LLM_PAUSE รอบ (ใช้กฎล้วน ไม่เสียเวลารอ) แล้วลองใหม่
LLM_FAIL_LIMIT = 2
LLM_PAUSE = 2


def ab_mode(tag: str) -> str:
    """[T26] ทดลอง A/B (AB_TEST=1, ตลาดซ้อม): สลับเกมละโหมด hybrid / rules · เกมเดิม (รีสตาร์ท) ใช้โหมดเดิม"""
    f = LOGS / "ab_modes.json"
    try:
        modes = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        modes = {}
    if tag not in modes:
        last = list(modes.values())[-1] if modes else "rules"
        modes[tag] = "rules" if last == "hybrid" else "hybrid"
        LOGS.mkdir(exist_ok=True)
        f.write_text(json.dumps(modes, ensure_ascii=False, indent=1), encoding="utf-8")
    return modes[tag]


def play(api: Market, llm: LLM | None, forever: bool) -> None:
    info = api.calendar()
    rules = info["rules"]
    memory, tv, acted, game = None, {}, set(), None
    rng = random.Random()
    llm_fails, llm_pause = 0, 0
    ab = os.environ.get("AB_TEST") == "1" and llm is not None
    game_mode = "hybrid"
    while True:
        try:
            state = api.state()
        except Exception as e:                      # noqa: BLE001 — never die on a network error
            log("state_error", error=str(e))
            time.sleep(3)
            continue
        # the sandbox reuses lot ids every game, so the samples are what tell one game from the next
        lots_key = tuple(sorted((l["lot_id"], json.dumps(l["sample"], sort_keys=True)) for l in state["lots"]))
        if lots_key != game:                        # new game (or sandbox reset): start fresh
            game, acted, tv = lots_key, set(), estimate_lots(rules, state["lots"])
            tag = hashlib.sha1(repr(lots_key).encode()).hexdigest()[:8]
            mpath = LOGS / f"memory-{state['market']}-{tag}.json"
            # [T26] sandbox อาจแจกล็อตชุดเดิมซ้ำ -> ไฟล์ความจำชื่อซ้ำกับเกมเก่า
            # ถ้ายังไม่ส่ง appraisal = เกมเพิ่งเริ่ม -> เก็บไฟล์เก่าไว้ แล้วเริ่มความจำใหม่
            # (ถ้าส่งแล้ว = agent รีสตาร์ทกลางเกม -> ใช้ความจำเดิมต่อ)
            if mpath.exists() and not state.get("appraisal_submitted"):
                mpath.rename(mpath.with_name(f"{mpath.stem}.old{int(time.time())}.json"))
            memory = Memory(mpath)
            game_mode = ab_mode(tag) if ab and state.get("market") == "sandbox" else "hybrid"
            log("new_game", market=state["market"], lots=len(state["lots"]), tv=tv,
                **({"ab_mode": game_mode} if ab else {}))
            dash.start(state.get("team_id", "?"), "T26 ขั้นบันได + เรียนรู้รายผู้ซื้อ", str(api.http.base_url), tv)
            if pi_view.enabled():                   # [T26] แสดงอย่างเดียว: ค่าจาก Pi ไม่เข้า appraisal/strategy
                pi_view.set_market_lots([l["lot_id"] for l in state["lots"]])
                pi_view.start()
        phase = state["phase"]

        if phase == "APPRAISAL" and not state["appraisal_submitted"]:
            try:
                api.appraise(appraisal_items(rules, state["lots"], tv))
                log("appraisal_sent", tv=tv)
            except MarketError as e:
                log("appraisal_error", error=str(e))

        elif phase == "ROUND_OPEN" and not state["paused"] and state["round"]["round_id"] not in acted:
            rid = state["round"]["round_id"]
            acted.add(rid)
            if not state.get("appraisal_submitted"):
                # [T26] พลาดช่วง APPRAISAL (เกม sandbox 04:24 ได้ 0/20) -> ลองส่งผลประเมินตอนนี้ ตลาดอาจรับหรือปฏิเสธ บันทึกผลไว้ทั้งสองกรณี
                try:
                    api.appraise(appraisal_items(rules, state["lots"], tv))
                    log("appraisal_late_sent", round=rid, tv=tv)
                except MarketError as e:
                    log("appraisal_late_error", round=rid, error=str(e))
            memory.record_results(state["last_results"])
            dash.learning(state["last_results"], memory)
            dash.round_open(state, rules)
            if pi_view.enabled():
                dash.sensors(state)                 # อยู่ใน try ของ dashboard: Pi พังไม่ทำให้ agent ล่ม
            if any(o["round_id"] == rid for o in state["my_open_offers"]):
                # restarted after sending this round: the market already holds our offers, and a new batch
                # would replace them (capacity_left_kg no longer counts them, so it would offer less)
                log("round_already_sent", round=rid, n=len(state["my_open_offers"]))
                continue
            use_llm = llm
            if game_mode == "rules":
                use_llm = None                                  # [T26] A/B: เกมนี้เป็นเกมกฎล้วน
            elif llm is not None and strategy.LLM_MODE == "hybrid" and llm_pause > 0:
                use_llm, llm_pause = None, llm_pause - 1          # [T26] พัก LLM รอบนี้ ใช้กฎล้วน
            ctx = strategy.Context(rules, info["calendar"], tv, rng, memory, use_llm, state["seconds_left"] or 999)
            t0 = time.time()
            try:
                offers, source = strategy.decide(state, ctx)
            except Exception as e:                  # noqa: BLE001 — a bug in strategy must not stop the agent
                log("strategy_crashed", error=repr(e))
                offers, source = strategy.guard(state, ctx, strategy.rule_plan(state, ctx, strategy.candidates(state, ctx))), "rules"
            if llm is not None and game_mode == "rules":
                source = "rules (A/B เกมกฎล้วน)"
            elif llm is not None and use_llm is None:
                source = "rules (พัก LLM)"
            elif use_llm is not None and strategy.LLM_MODE == "hybrid":
                llm_fails = llm_fails + 1 if "LLM ไม่ตอบ" in source else 0
                if llm_fails >= LLM_FAIL_LIMIT:
                    llm_fails, llm_pause = 0, LLM_PAUSE
                    log("llm_paused", rounds=LLM_PAUSE, error=llm.last_error)
            dash.decision(state, offers, source, (time.time() - t0) * 1000, strategy.MIN_MARKUP)
            if use_llm is not None:
                dash.llm_round(state, ctx.llm_info, source, offers, llm)
            t1 = time.time()
            sent = send_offers(api, offers) if offers else []
            dash.action(state, len(offers), len(sent), (time.time() - t1) * 1000)
            memory.record_sent(rid, offers, state, tv)
            log("offers_sent", round=rid, source=source, n=len(sent), kg=sum(o["kg"] for o in sent),
                seconds=round(time.time() - t0, 1), offers=offers, notes=ctx.notes)

        elif phase == "CLOSED" and game is not None and "closed" not in acted and not acted and not forever:
            # [T26] เริ่ม agent ตอนตลาดยังปิด (เกมก่อนจบแล้ว / ทีมงานยังไม่กด Start) -> รอเกมใหม่ ไม่จบงานทันที
            if "waiting" not in acted:
                log("waiting_for_market", phase=phase)
            acted.add("waiting")

        elif phase == "CLOSED" and game is not None and "closed" not in acted and acted - {"waiting"}:
            acted.add("closed")
            memory.record_results(state["last_results"])
            log("market_closed", standings=state["standings"])
            dash.learning(state["last_results"], memory)
            score = None
            if state.get("market") == "sandbox":            # ตลาดฝึกเท่านั้น: ดูคะแนนจริงหลังปิดตลาด
                try:
                    rev = api.call("GET", "/v1/sandbox/reveal")
                    score = rev.get("score")
                    with open(LOGS / "reveal.jsonl", "a", encoding="utf-8") as rf:   # [T26] ค่าจริงเทียบค่าตัวอย่างของทุกเกม ไว้ศึกษา noise ของ Sample
                        rf.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "lots": rev.get("lots"), "score": score}, ensure_ascii=False) + "\n")
                except Exception:                           # noqa: BLE001
                    pass
                if score:                                   # [T26] เก็บคะแนนจริงทุกเกมไว้เทียบโหมด
                    log("sandbox_score", mode=game_mode if ab else ("hybrid" if llm else "rules"),
                        pr=score.get("pr"), total=score.get("total"), rank=score.get("rank"),
                        revenue=state["standings"].get("revenue_thb"), score=score)
            dash.end(state["standings"], score)
            if not forever:
                return

        # Read the clock from the server; never hard-code round times.
        left = state["seconds_left"] or 5
        time.sleep(min(2.0, max(0.5, left + 0.2)))      # [T26] เช็กถี่ขึ้น (5→2 วิ ≈0.5 คำขอ/วิ ไกลจากเพดาน 5/วิ) เริ่มรอบเปิดช้าลงน้อยลง


def main() -> None:
    load_env(HERE / ".env")
    api = Market(os.environ["MARKET_URL"], os.environ["TEAM_KEY"])
    llm = None
    # [T26] โหมด: USE_LLM=0 กฎล้วน · USE_LLM=1 + LLM_MODE=hybrid กฎ+LLM · USE_LLM=1 + LLM_MODE=llm LLM ล้วน
    strategy.LLM_MODE = os.environ.get("LLM_MODE", "hybrid")
    if os.environ.get("USE_LLM", "1") == "1":
        key = os.environ.get("LLM_API_KEY", "")
        if not key or "change-me" in key:              # gateway ใช้ key เดียวกับตลาด (คู่มือ)
            key = os.environ["TEAM_KEY"]
        llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], key,
                  timeout=float(os.environ.get("LLM_TIMEOUT", "60")), thinking=os.environ.get("LLM_THINKING") == "1",
                  max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "1500")),
                  temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")))
        strategy.LLM_HISTORY = int(os.environ.get("LLM_HISTORY", "3"))
        extra = HERE / "llm_prompt_extra.txt"                      # คำสั่งเพิ่มเติมถึง LLM (แก้ได้จาก dashboard)
        strategy.EXTRA_PROMPT = extra.read_text(encoding="utf-8") if extra.exists() else ""
        problem = llm_url_problem(os.environ.get("LLM_BASE_URL", ""), os.environ["TEAM_KEY"])
        if problem and strategy.LLM_MODE == "hybrid":
            # [T26] กฎ + LLM แต่ที่อยู่ gateway ใช้ไม่ได้ -> ใช้ agent กฎล้วนทั้งเกม ไม่ต้องลองต่อ LLM ทุกรอบ
            log("llm_disabled", reason=problem, mode="rules")
            llm = None
    play(api, llm, "--forever" in sys.argv)


if __name__ == "__main__":
    main()
