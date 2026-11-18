#!/usr/bin/env bash
# pi_setup.sh: รันบน Raspberry Pi 5 ครั้งเดียว (ผ่าน SSH)  →  bash ~/coffee-pi/pi_setup.sh
# ติดตั้ง Python env + เปิด I2C + ตั้งให้ pi_server รันเองทุกครั้งที่เปิดเครื่อง
set -e
cd ~/coffee-pi

echo "== 1) อัปเดตระบบ + แพ็กเกจที่ต้องใช้"
sudo apt update
sudo apt install -y python3-venv python3-picamera2 i2c-tools python3-lgpio

echo "== 2) เปิด I2C (สำหรับ ADS1115 ตัวแปลง analog ของเซนเซอร์ความชื้น)"
sudo raspi-config nonint do_i2c 0

echo "== 3) venv (--system-site-packages เพื่อใช้ picamera2/lgpio ที่ลงผ่าน apt ได้)"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install fastapi uvicorn
# เซนเซอร์จริง เปิดบรรทัดที่ใช้:
# .venv/bin/pip install adafruit-circuitpython-ads1x15   # ความชื้นแบบ analog
# .venv/bin/pip install pyserial                         # เครื่องวัดความชื้นแบบ USB
# .venv/bin/pip install ultralytics                      # โมเดล YOLO (หนัก ใช้เวลานาน)

echo "== 4) ตั้ง service ให้รันอัตโนมัติ"
sed "s/__USER__/$USER/g" coffee-pi.service | sudo tee /etc/systemd/system/coffee-pi.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now coffee-pi
sleep 3
curl -s localhost:8100/health && echo
echo "== เสร็จ  IP ของเครื่องนี้: $(hostname -I)"
