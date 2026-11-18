"""
dashboard.py: หน้าต่างดูการทำงานของ agent แบบสด (ไม่ต้องใช้อินเทอร์เน็ต)

    cd ~/tesa-starter && .venv/bin/python -m dashboard.dashboard            → เปิด http://127.0.0.1:8050
    .venv/bin/python -m dashboard.dashboard --port 9000 --no-open

อ่าน dashboard/live/events.jsonl ที่ agent เขียน แล้วส่งให้หน้าเว็บ (index.html) ดึงทุก ~0.7 วินาที
ใช้แค่ standard library ของ Python
"""
import argparse
import json
import os
import re
import signal
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
EVENTS = HERE / "live" / "events.jsonl"
INDEX = HERE / "index.html"

# ---------------- [T26] แผงควบคุม: ใส่ key / ตรวจ / เริ่ม / หยุด agent จากหน้าเว็บ ----------------
# ความปลอดภัย: รับคำสั่งเฉพาะจากเครื่องนี้ (127.0.0.1) + ต้องมี header X-T26 (กันเว็บอื่นแอบสั่ง)
#             ไม่ส่ง key กลับไปที่หน้าเว็บ (บอกแค่ 4 ตัวท้าย) · หน้าเว็บนี้สั่งได้แค่ เริ่ม/หยุด agent
#             ยื่นราคาด้วยมือไม่ได้ (กติกาห้ามส่งคำสั่งด้วยมือระหว่างแข่ง)
STARTER = Path(os.environ.get("T26_STARTER", str(HERE.parent))).expanduser()   # [T26] dashboard อยู่ใน tesa-starter/dashboard
CONSOLE = Path("/tmp/t26-run-console.txt")
_proc: subprocess.Popen | None = None
_started: float | None = None
_stopped = False                                # หยุดเพราะผู้ใช้กดหยุด (ไม่ใช่ล่ม)
_last_check = ""
_last_llm = ""


def _read_env() -> dict:
    env = {}
    p = STARTER / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict) -> None:
    p = STARTER / ".env"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    for k, v in updates.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == k and not line.lstrip().startswith("#"):
                lines[i] = f"{k}={v}"
                break
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)                              # อ่านได้เฉพาะเจ้าของเครื่อง


def _external_agent() -> bool:
    r = subprocess.run(["pgrep", "-f", "[P]ython main.py"], capture_output=True)
    return r.returncode == 0


def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def control_status() -> dict:
    env = _read_env()
    key = env.get("TEAM_KEY", "")
    tail = []
    if CONSOLE.exists():
        tail = CONSOLE.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    return {"market_url": env.get("MARKET_URL", ""), "key_tail": key[-4:] if key else "",
            "use_llm": env.get("USE_LLM", "1"), "running": _running(),
            "mode": "rules" if env.get("USE_LLM", "1") != "1" else ("llm" if env.get("LLM_MODE") == "llm" else "hybrid"),
            "llm_base_url": env.get("LLM_BASE_URL", ""), "llm_model": env.get("LLM_MODEL", "event-llm"),
            "llm_timeout": env.get("LLM_TIMEOUT", "60"),
            "llm_key_tail": (lambda k: k[-4:] if k and "change-me" not in k else "")(env.get("LLM_API_KEY", "")),
            "llm_test": _last_llm,
            "llm_temperature": env.get("LLM_TEMPERATURE", "0.7"), "llm_max_tokens": env.get("LLM_MAX_TOKENS", "1500"),
            "llm_thinking": env.get("LLM_THINKING", "0"), "llm_history": env.get("LLM_HISTORY", "3"),
            "llm_extra": (STARTER / "llm_prompt_extra.txt").read_text(encoding="utf-8")
                         if (STARTER / "llm_prompt_extra.txt").exists() else "",
            "external": (not _running()) and _external_agent(),
            "started": _started, "stopped": _stopped, "exit_code": None if _proc is None else _proc.poll(),
            "console": tail, "check": _last_check, "starter": str(STARTER)}


def control_env(body: dict) -> dict:
    up = {}
    url = (body.get("market_url") or "").strip()
    key = (body.get("team_key") or "").strip()
    if url:
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-:]+(/[^\s]*)?", url):
            return {"ok": False, "msg": "ที่อยู่ตลาดไม่ถูกรูปแบบ (ต้องขึ้นต้นด้วย http://)"}
        up["MARKET_URL"] = url.rstrip("/")
    if key:
        if not re.fullmatch(r"[\x21-\x7e]{6,200}", key):
            return {"ok": False, "msg": "key ไม่ถูกรูปแบบ (ห้ามมีช่องว่าง)"}
        up["TEAM_KEY"] = key
    mode = (body.get("mode") or "").strip()
    if mode:
        if mode not in ("rules", "hybrid", "llm"):
            return {"ok": False, "msg": "โหมดไม่ถูกต้อง"}
        up["USE_LLM"] = "0" if mode == "rules" else "1"
        if mode != "rules":
            up["LLM_MODE"] = mode
    lurl = (body.get("llm_base_url") or "").strip()
    if lurl:
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-:]+(/[^\s]*)?", lurl):
            return {"ok": False, "msg": "LLM Base URL ไม่ถูกรูปแบบ (เช่น http://x.x.x.x:4000/v1)"}
        up["LLM_BASE_URL"] = lurl.rstrip("/")
    model = (body.get("llm_model") or "").strip()
    if model:
        if not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,80}", model):
            return {"ok": False, "msg": "ชื่อ model ไม่ถูกรูปแบบ"}
        up["LLM_MODEL"] = model
    tmo = str(body.get("llm_timeout") or "").strip()
    if tmo:
        if not re.fullmatch(r"\d{1,3}", tmo) or not 5 <= int(tmo) <= 200:
            return {"ok": False, "msg": "Timeout ต้องเป็น 5–200 วินาที"}
        up["LLM_TIMEOUT"] = tmo
    lkey = (body.get("llm_api_key") or "").strip()
    if lkey:
        if not re.fullmatch(r"[\x21-\x7e]{6,200}", lkey):
            return {"ok": False, "msg": "LLM API KEY ไม่ถูกรูปแบบ"}
        up["LLM_API_KEY"] = lkey
    for field, envk, lo, hi, kind in (("llm_temperature", "LLM_TEMPERATURE", 0.0, 1.5, float),
                                      ("llm_max_tokens", "LLM_MAX_TOKENS", 100, 2000, int),
                                      ("llm_history", "LLM_HISTORY", 1, 8, int)):
        v = str(body.get(field) or "").strip()
        if v:
            try:
                x = kind(v)
            except ValueError:
                return {"ok": False, "msg": f"{envk} ต้องเป็นตัวเลข"}
            if not lo <= x <= hi:
                return {"ok": False, "msg": f"{envk} ต้องอยู่ระหว่าง {lo}–{hi}"}
            up[envk] = str(x)
    if body.get("llm_thinking") in ("0", "1"):
        up["LLM_THINKING"] = body["llm_thinking"]
    if "llm_extra" in body and body["llm_extra"] is not None:
        txt = str(body["llm_extra"])[:3000]
        (STARTER / "llm_prompt_extra.txt").write_text(txt, encoding="utf-8")
        up.setdefault("_extra", "1")
    if up.get("USE_LLM") == "1":
        cur = {**_read_env(), **up}
        if "GATEWAY-ADDRESS" in cur.get("LLM_BASE_URL", "GATEWAY-ADDRESS"):
            return {"ok": False, "msg": "โหมดที่ใช้ LLM ต้องใส่ LLM Base URL (ที่อยู่ gateway ที่ทีมงานแจ้ง) ก่อน"}
    if not up:
        return {"ok": False, "msg": "ยังไม่ได้กรอกอะไร"}
    if _running():
        return {"ok": False, "msg": "agent กำลังรันอยู่ หยุดก่อนแล้วค่อยเปลี่ยน key"}
    up.pop("_extra", None) and None
    if up:
        _write_env(up)
    if not up and "llm_extra" in body:
        return {"ok": True, "msg": "บันทึกคำสั่งเพิ่มเติมถึง LLM แล้ว"}
    return {"ok": True, "msg": "บันทึกแล้ว: " + ", ".join(k for k in up if "KEY" not in k) + (" + key" if any("KEY" in k for k in up) else "")}


def control_check() -> dict:
    global _last_check
    py = STARTER / ".venv" / "bin" / "python"
    r = subprocess.run([str(py), "tools/preflight.py"], cwd=STARTER, capture_output=True, text=True, timeout=40)
    _last_check = (r.stdout + r.stderr).strip()
    return {"ok": r.returncode == 0, "msg": _last_check}


def control_llm_test(body: dict) -> dict:
    global _last_llm
    py = STARTER / ".venv" / "bin" / "python"
    args = [str(py), "tools/llm_try.py"] + (["--rules"] if body.get("rules") else [])
    try:
        r = subprocess.run(args, cwd=STARTER, capture_output=True, text=True, timeout=180)
        _last_llm = (r.stdout + r.stderr).strip()
        return {"ok": r.returncode == 0, "msg": _last_llm}
    except subprocess.TimeoutExpired:
        _last_llm = "❌ LLM ตอบช้าเกิน 180 วินาที"
        return {"ok": False, "msg": _last_llm}


def control_start(body: dict) -> dict:
    global _proc, _started, _stopped
    if _running() or _external_agent():
        return {"ok": False, "msg": "agent รันอยู่แล้ว"}
    args = ["/bin/bash", str(STARTER / "run.sh")] + (["--forever"] if body.get("forever") else [])
    out = CONSOLE.open("w", encoding="utf-8")
    _proc = subprocess.Popen(args, cwd=STARTER, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                             env={**os.environ, "NO_OPEN": "1", "PYTHONUNBUFFERED": "1"})
    _started = time.time()
    _stopped = False
    return {"ok": True, "msg": "เริ่ม agent แล้ว" + (" (โหมดซ้อม เล่นต่อเรื่อย ๆ)" if body.get("forever") else "")}


def control_stop() -> dict:
    global _stopped
    _stopped = True
    if _running():
        os.killpg(_proc.pid, signal.SIGINT)         # เหมือนกด Ctrl+C
        for _ in range(50):
            if _proc.poll() is not None:
                break
            time.sleep(0.1)
        if _proc.poll() is None:
            os.killpg(_proc.pid, signal.SIGTERM)
        return {"ok": True, "msg": "หยุด agent แล้ว"}
    if _external_agent():
        subprocess.run(["pkill", "-INT", "-f", "[P]ython main.py"])
        return {"ok": True, "msg": "หยุด agent ที่รันจาก Terminal แล้ว"}
    return {"ok": False, "msg": "ไม่มี agent รันอยู่"}


def read_events(after: int, run: str) -> dict:
    if not EVENTS.exists():
        return {"run": "", "events": []}
    lines = EVENTS.read_text(encoding="utf-8").splitlines()
    evs = []
    for ln in lines:
        try:
            evs.append(json.loads(ln))
        except ValueError:
            pass                                # บรรทัดที่กำลังเขียนไม่เสร็จ รอบหน้าค่อยอ่าน
    cur = evs[0]["run"] if evs else ""
    if run != cur:                              # เกมใหม่ → ส่งทั้งหมดตั้งแต่ต้น
        after = 0
    return {"run": cur, "events": [e for e in evs if e["seq"] > after]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/events":
            q = parse_qs(u.query)
            data = read_events(int(q.get("after", ["0"])[0]), q.get("run", [""])[0])
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/control/status":
            if not self._trusted():
                return self._send(403, b"forbidden", "text/plain")
            self._json(control_status())
        else:
            self._send(404, b"not found", "text/plain")

    def _trusted(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-T26") == "1"

    def _json(self, data: dict, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        if not self._trusted():
            return self._send(403, b"forbidden", "text/plain")
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        try:
            path = urlparse(self.path).path
            if path == "/api/control/env":
                self._json(control_env(body))
            elif path == "/api/control/check":
                self._json(control_check())
            elif path == "/api/control/llmtest":
                self._json(control_llm_test(body))
            elif path == "/api/control/start":
                self._json(control_start(body))
            elif path == "/api/control/stop":
                self._json(control_stop())
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:                      # noqa: BLE001
            self._json({"ok": False, "msg": f"ผิดพลาด: {e!r}"}, 500)

    def log_message(self, *a):                 # ไม่พิมพ์ log ทุก request
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = ให้เครื่องอื่นในวงแลนเปิดดูได้")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    print(f"Dashboard: {url}   (Ctrl+C เพื่อปิด)")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
