#!/bin/bash
# [T26] run.sh: สั่งครั้งเดียว ทุกอย่างทำงาน
#
#   ~/tesa-starter/run.sh            แข่งจริง: ตรวจความพร้อม → เปิด dashboard → รัน agent จนตลาดปิด
#   ~/tesa-starter/run.sh --forever  ตลาดซ้อม: เล่นเกมใหม่ต่อไปเรื่อย ๆ
#   ~/tesa-starter/run.sh --check    ตรวจความพร้อมอย่างเดียว ไม่รัน agent
#   ~/tesa-starter/run.sh --clean    ย้าย log เก่าออกก่อนเริ่ม (ใช้ตอนเช้าก่อนแข่ง)
#
# ระหว่างรัน: เครื่องจะไม่หลับ (caffeinate) · agent ล่มจะรีสตาร์ทเองใน 3 วินาที (ใช้ความจำเดิมต่อ)
# กด Ctrl+C เพื่อหยุดทั้งหมด

HERE="$(cd "$(dirname "$0")" && pwd)"
DASH_DIR="$HERE"                                # [T26] dashboard อยู่ใน tesa-starter/dashboard
PY="$HERE/.venv/bin/python"
cd "$HERE" || exit 1

AGENT_ARGS=()
CHECK_ONLY=0
PRE_ARGS=()
for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --clean) PRE_ARGS+=("--clean") ;;
    *) AGENT_ARGS+=("$a") ;;
  esac
done

# 1) ตรวจความพร้อม
"$PY" tools/preflight.py "${PRE_ARGS[@]}" || { echo "❌ ยังไม่พร้อม แก้ตามข้อความข้างบนก่อน"; exit 1; }
mkdir -p "$HERE/logs"

# 2) dashboard (ถ้ายังไม่เปิด)
if ! lsof -iTCP:8050 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  DPY="$DASH_DIR/.venv/bin/python"; [ -x "$DPY" ] || DPY="python3"
  (cd "$DASH_DIR" && nohup "$DPY" -m dashboard.dashboard --no-open > /tmp/t26-dashboard.log 2>&1 &)
  sleep 1
  echo "📊 เปิด dashboard แล้ว: http://127.0.0.1:8050"
else
  echo "📊 dashboard เปิดอยู่แล้ว: http://127.0.0.1:8050"
fi
[ -z "${NO_OPEN:-}" ] && open "http://127.0.0.1:8050" 2>/dev/null

if [ "$CHECK_ONLY" = 1 ]; then
  echo "✅ ตรวจเสร็จ (--check ไม่รัน agent)"
  exit 0
fi

# 3) รัน agent: กันเครื่องหลับ + รีสตาร์ทเองถ้าล่ม
trap 'echo; echo "⏹  หยุดตามคำสั่ง (Ctrl+C)"; exit 0' INT
echo "🤖 เริ่ม agent  $(date '+%H:%M:%S')  (Ctrl+C = หยุด)"
while true; do
  caffeinate -dims "$PY" main.py "${AGENT_ARGS[@]}"
  code=$?
  if [ $code -eq 0 ]; then
    echo "🏁 ตลาดปิดแล้ว agent จบงาน  $(date '+%H:%M:%S')"
    echo "   ถ้าทีมงานเรียกส่งโค้ด (ภายใน 10 นาที):  cd ~/tesa-starter && .venv/bin/python pack.py --send"
    break
  fi
  [ $code -eq 130 ] && break
  echo "⚠️  agent หยุดผิดปกติ (code $code) รีสตาร์ทใน 3 วินาที..."
  sleep 3
done
