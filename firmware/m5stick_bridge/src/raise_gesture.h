// Raise to talk: spots the Stick being brought up to look at, from the IMU,
// and main.cpp then starts the same hands-free turn the wake word does.
//
// "Raise" means a sequence, not a reading, so everyday motion doesn't fire it:
//   1. real movement (gyro or a departure from 1 g),
//   2. arriving in the looking-at-it pose (gravity within a cone around
//      `pose`, in the Stick's own axes) from somewhere clearly else,
//   3. then holding still there for `holdMs`, the pause when you look.
// Lying in the pose (on a stand), passing through it (an arm swing), drifting
// into it slowly, or fidgeting while already holding it up don't fire. After
// a fire it re-arms only once the Stick has left the pose again.
//
// The carry style varies (wrist, hand, lifted off a desk), which changes
// where the lift starts but not where it ends, so only the end pose is
// configured. A false raise is cheap by design: it starts listening, and if
// nobody speaks the turn ends on the Stick without sending anything.
//
// Plain C++ with timestamps passed in, like vad.h, so it's tested on the host
// (tests/test_firmware_raise.py). The thresholds are placeholders until
// they're fitted to recorded motion (firmware/imu_logger, tools/imu_record.py).
#pragma once
#include <cmath>
#include <cstdint>

namespace Raise {

struct Config {
  // Gravity direction (device axes, any length) with the screen facing you.
  float pose[3] = {0.0f, -0.7071f, -0.7071f};
  float poseConeDeg = 35.0f;
  // Leaving the pose by this much (from its centre) re-arms the detector,
  // and having been this far away recently is what "arriving" means.
  float farDeg = 60.0f;
  uint32_t arriveWindowMs = 2500;
  float motionGyroDps = 80.0f;
  float motionAccelG = 0.25f;
  // The movement must have ended at most this long before the hold began.
  uint32_t motionWindowMs = 1200;
  float stillGyroDps = 25.0f;
  float stillAccelG = 0.08f;
  uint32_t holdMs = 400;
  uint32_t cooldownMs = 3000;
};

class Detector {
 public:
  explicit Detector(const Config &config = Config()) : cfg_(config) {
    float n = std::sqrt(cfg_.pose[0] * cfg_.pose[0] + cfg_.pose[1] * cfg_.pose[1] +
                        cfg_.pose[2] * cfg_.pose[2]);
    for (float &v : cfg_.pose) v /= (n > 0 ? n : 1.0f);
  }

  // One IMU sample: accel in g, gyro in deg/s. True when a raise completes.
  bool feed(float ax, float ay, float az, float gx, float gy, float gz, uint32_t ms) {
    float amag = std::sqrt(ax * ax + ay * ay + az * az);
    float gmag = std::sqrt(gx * gx + gy * gy + gz * gz);
    angle_ = 180.0f;
    if (amag > 0.2f) {
      float c = (ax * cfg_.pose[0] + ay * cfg_.pose[1] + az * cfg_.pose[2]) / amag;
      c = c > 1.0f ? 1.0f : (c < -1.0f ? -1.0f : c);
      angle_ = std::acos(c) * 57.29578f;
    }
    float accelDev = std::fabs(amag - 1.0f);
    bool moving = gmag > cfg_.motionGyroDps || accelDev > cfg_.motionAccelG;
    bool still = gmag < cfg_.stillGyroDps && accelDev < cfg_.stillAccelG;
    bool inPose = angle_ <= cfg_.poseConeDeg;

    if (moving) { lastMotionMs_ = ms; seenMotion_ = true; }
    if (angle_ >= cfg_.farDeg) { lastFarMs_ = ms; seenFar_ = true; armed_ = true; }

    if (!(inPose && still)) {
      holding_ = false;
      return false;
    }
    if (!holding_) {
      holding_ = true;
      holdStartMs_ = ms;
    }
    bool held = ms - holdStartMs_ >= cfg_.holdMs;
    bool liftedIn = seenMotion_ && holdStartMs_ - lastMotionMs_ <= cfg_.motionWindowMs;
    bool arrived = seenFar_ && holdStartMs_ - lastFarMs_ <= cfg_.arriveWindowMs;
    // Judged from when the hold began: a raise that starts inside the cooldown
    // is ignored, not fired late when the cooldown runs out mid-hold.
    bool cooled = !fired_ || holdStartMs_ - lastFireMs_ >= cfg_.cooldownMs;
    if (held && liftedIn && arrived && armed_ && cooled) {
      armed_ = false;
      fired_ = true;
      lastFireMs_ = ms;
      return true;
    }
    return false;
  }

  // Degrees between gravity and the pose, for logging near misses.
  float poseAngle() const { return angle_; }

 private:
  Config cfg_;
  float angle_ = 180.0f;
  bool holding_ = false, armed_ = false, fired_ = false, seenMotion_ = false, seenFar_ = false;
  uint32_t holdStartMs_ = 0, lastMotionMs_ = 0, lastFarMs_ = 0, lastFireMs_ = 0;
};

}  // namespace Raise
