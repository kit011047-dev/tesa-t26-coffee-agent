/*
 * coffee_sensor.ino : ESP32 อ่านเซนเซอร์แล้วส่ง JSON ทีละบรรทัดทาง USB Serial ไปให้ Raspberry Pi
 *
 * รูปแบบที่ส่ง (1 บรรทัด / ครั้ง):
 *   {"moisture_pct":11.8,"weight_kg":512.3,"raw_moist":2140,"raw_weight":-48211,"sim":1,"ms":12345}
 *
 * คำสั่งที่รับจาก Pi (พิมพ์แล้วกด Enter):
 *   READ   → ส่งค่า 1 บรรทัดทันที
 *   TARE   → ตั้งศูนย์ตาชั่ง (ไม่มีของบนตาชั่ง)
 *   PING   → ตอบ {"pong":1}
 * และส่งเองอัตโนมัติทุก SEND_EVERY_MS
 *
 * อัปโหลด: Arduino IDE → Board "ESP32 Dev Module" → Port (CH9102 / usbmodem) → Upload
 */

#define SIMULATE 1            // 1 = ยังไม่มีเซนเซอร์ ส่งค่าจำลอง | 0 = อ่านเซนเซอร์จริง
#define USE_HX711 0           // 1 = มี load cell + HX711 (ต้องลง library "HX711" by Bogdan Necula)

const int  PIN_MOIST = 34;    // ขา analog ของเซนเซอร์ความชื้น (ADC1: 32-39 ใช้คู่ Wi-Fi ได้)
const int  PIN_HX_DT = 16;    // HX711 DOUT
const int  PIN_HX_SCK = 4;    // HX711 SCK
const unsigned long SEND_EVERY_MS = 1000;

// ---------- ค่า calibrate (ต้องวัดจริงแล้วแก้) ----------
// ความชื้น % = MOIST_A * raw + MOIST_B   (หาจากเมล็ดที่รู้ความชื้น 2 ค่าขึ้นไป)
float MOIST_A = -0.0040, MOIST_B = 20.5;
// น้ำหนัก กก. = (raw - offset) / SCALE   (TARE หา offset, วางของรู้น้ำหนักหา SCALE)
float HX_SCALE = 21000.0;

#if USE_HX711
#include "HX711.h"
HX711 scale;
#endif

long hxOffset = 0;
unsigned long lastSend = 0;

int readMoistRaw() {
  long sum = 0;
  for (int i = 0; i < 32; i++) { sum += analogRead(PIN_MOIST); delay(2); }   // เฉลี่ยลด noise
  return sum / 32;
}

long readWeightRaw() {
#if USE_HX711
  return scale.is_ready() ? scale.read_average(10) : 0;
#else
  return 0;
#endif
}

void sendReading() {
  float moist, kg;
  long rawW = 0; int rawM = 0;
  if (SIMULATE) {
    moist = 11.0 + (esp_random() % 250) / 100.0;        // 11.00–13.49
    kg = 300 + (esp_random() % 1200);
  } else {
    rawM = readMoistRaw();
    rawW = readWeightRaw();
    moist = MOIST_A * rawM + MOIST_B;
    kg = (rawW - hxOffset) / HX_SCALE;
  }
  Serial.printf("{\"moisture_pct\":%.2f,\"weight_kg\":%.2f,\"raw_moist\":%d,\"raw_weight\":%ld,\"sim\":%d,\"ms\":%lu}\n",
                moist, kg, rawM, rawW, SIMULATE, millis());
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
#if USE_HX711
  scale.begin(PIN_HX_DT, PIN_HX_SCK);
#endif
  delay(200);
  Serial.println("{\"boot\":1,\"fw\":\"coffee_sensor v1\"}");
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim(); cmd.toUpperCase();
    if (cmd == "READ") sendReading();
    else if (cmd == "PING") Serial.println("{\"pong\":1}");
    else if (cmd == "TARE") { hxOffset = readWeightRaw(); Serial.printf("{\"tare\":%ld}\n", hxOffset); }
  }
  if (millis() - lastSend >= SEND_EVERY_MS) {
    lastSend = millis();
    sendReading();
  }
}
