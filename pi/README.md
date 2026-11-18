# ส่วนที่ 8: เชื่อม agent กับ Raspberry Pi (เซนเซอร์ + ผลนับเมล็ดจาก YOLO)

```
Track 2 (Jet): inbox/<lot_id>/*.jpg → run_system.py (YOLO) → ~/tesa_work/outbox/<lot_id>.json
                                                                      │
ESP32 (ความชื้น/น้ำหนัก) ──USB──→ Raspberry Pi: pi_server.py :8100 ←──┘
                                          │  HTTP JSON ผ่านวงแลน
                                          ↓
                       Mac: agent.py + pi_client.py → server ตลาด
```

## โหมดของ pi_server (ตัวแปร SENSOR_MODE)
| โหมด | ความชื้น / น้ำหนัก | จำนวนเมล็ด |
|---|---|---|
| `fake` | สุ่ม | สุ่ม |
| `yolo` (**ใช้อยู่ตอนนี้**) | ไม่มี (บอกว่า missing) | จาก outbox ของ Track 2 |
| `esp32` | จาก ESP32 | จาก outbox ของ Track 2 |

สลับโหมดบน Pi: แก้ `Environment=SENSOR_MODE=...` ใน `/etc/systemd/system/coffee-pi.service`
แล้ว `sudo systemctl daemon-reload && sudo systemctl restart coffee-pi`

เช็คจาก Mac: `curl http://172.16.11.205:8100/health` → บอกโหมด และรายชื่อล็อตที่ YOLO นับแล้ว

## วันแข่ง: ฝั่ง Track 2 ต้องทำ
```bash
cd ~/tesa_work
~/tesa/bin/python day3/run_system.py --model deploy/day3_r4 --inbox inbox --outbox outbox --watch
```
- **ชื่อโฟลเดอร์ใน inbox ต้องตรงกับ lot_id ของตลาดเป๊ะ** (เช่น `inbox/A-1/`)
- เปลี่ยนโมเดลได้ตลอด (`--model deploy/<ใหม่>`) ฝั่ง agent ไม่ต้องแก้อะไร

## ตั้งค่าฝั่ง agent (`.env ของ tesa-starter (PI_ENABLED, PI_URL, PI_SOURCE)`)
| ค่า | ความหมาย |
|---|---|
| `PI_ENABLED = True` | เปิดใช้ Pi |
| `PI_URL` | IP ของ Pi (เปลี่ยนวงแลนวันแข่งต้องแก้ตรงนี้: บน Pi สั่ง `hostname -I`) |
| `PI_OVERRIDE = ("bean_counts",)` | ผลนับเมล็ดจาก YOLO ใช้แทนของ server เสมอ |
| `PI_MIN_BEANS = 30` | YOLO นับได้น้อยกว่านี้ → ไม่เชื่อ |
| `PI_MAX_DEFECT_DIFF = 20` | % เสียจาก YOLO ต่างจาก server เกิน 20 จุด → ไม่เชื่อ |

## หลักความปลอดภัย
- Pi ล่ม / ช้าเกิน `PI_TIMEOUT` / ค่านอกช่วง / ยังไม่มีผลล็อตนั้น → ใช้ข้อมูลจาก server ตลาดตามเดิม
- **ตัวกันพังผล YOLO**: ถ้าผลน่าสงสัย (นับน้อยไป หรือต่างจาก server มาก) จะไม่ใช้ และขึ้นเตือนในช่องกำกับดูแลของ dashboard
  (ทดสอบจริง: รูปตัวอย่างที่ไม่ใช่ล็อตจริงทำให้ YOLO บอกเสีย ~100% → ถ้าไม่มีตัวกัน agent ไม่ขายอะไรเลย คะแนน 50 แทน 93.5)
- เช็คความสดของค่าด้วยนาฬิกาของ Mac ไม่ใช้ของ Pi (Pi ไม่มีถ่านนาฬิกา เคยเพี้ยน 2 ชม. 24 นาที)
- ค่าเดิมของ server เก็บไว้ใน `_server_values` เทียบได้

## ซ้อมบน Mac (ไม่ต้องมี Pi)
```bash
cd pi && SENSOR_MODE=fake python -m uvicorn pi_server:app --port 8100     # Terminal 1
(ฝั่ง agent ดู pi_view.py ใน tesa-starter)                                                     # Terminal 2
```
