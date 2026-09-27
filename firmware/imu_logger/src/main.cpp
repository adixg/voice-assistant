// One line per IMU sample, 50 Hz: "ms ax ay az gx gy gz" (g, deg/s), the
// format tests/test_firmware_raise.py feeds raise_gesture.h. Lines starting
// with '#' are comments. The screen shows the live gravity vector.
#include <M5Unified.h>

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  Serial.begin(115200);
  M5.Display.setTextSize(2);
  delay(1500);
  Serial.printf("# imu_logger: imu=%d (0 none, else M5Unified imu_t)\n", (int)M5.Imu.getType());
}

void loop() {
  static uint32_t next = 0;
  uint32_t now = millis();
  if ((int32_t)(now - next) < 0) return;
  next = now + 20;
  M5.Imu.update();
  auto d = M5.Imu.getImuData();
  Serial.printf("%lu %.4f %.4f %.4f %.2f %.2f %.2f\n", (unsigned long)now, d.accel.x, d.accel.y,
                d.accel.z, d.gyro.x, d.gyro.y, d.gyro.z);
  static uint32_t lastDraw = 0;
  if (now - lastDraw > 200) {
    lastDraw = now;
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setCursor(4, 10);
    M5.Display.printf("x %+.2f\ny %+.2f\nz %+.2f", d.accel.x, d.accel.y, d.accel.z);
  }
}
