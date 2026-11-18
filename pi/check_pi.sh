#!/usr/bin/env bash
# check_pi.sh: รันบน Mac เช็กว่าต่อ Pi ได้ครบทุกชั้น   →  bash part8/check_pi.sh coffeepi.local
HOST="${1:-coffeepi.local}"
echo "1) ping (เครือข่ายถึงกันไหม)";       ping -c 2 -t 3 "$HOST" > /dev/null && echo "   ok" || echo "   ไม่ผ่าน: คนละวง Wi-Fi / Pi ยังไม่บูต / เครือข่ายกั้นเครื่องคุยกัน"
echo "2) SSH พอร์ต 22";                    nc -z -G 3 "$HOST" 22 && echo "   ok" || echo "   ไม่ผ่าน: ยังไม่เปิด SSH ตอนลง OS"
echo "3) API พอร์ต 8100";                  curl -s -m 3 "http://$HOST:8100/health" && echo || echo "   ไม่ผ่าน: pi_server ไม่ได้รัน (ssh เข้าไปดู: sudo systemctl status coffee-pi)"
echo "4) วัดล็อตทดสอบ";                     curl -s -m 10 "http://$HOST:8100/measure?lot_id=TEST" && echo
