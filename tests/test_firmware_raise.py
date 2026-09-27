"""firmware/m5stick_bridge/src/raise_gesture.h, compiled and run on the host.

Motion here is synthetic: gravity turning from a start orientation into the
detector's pose at a given speed, sampled at 50 Hz like the firmware. The
cases are the ones a raise-to-talk has to get right in both directions: the
three carry styles (desk, hand, wrist) must fire; resting in the pose,
swinging through it, drifting into it and fidgeting in it must not.
"""
import math
import shutil
import subprocess
import textwrap
from pathlib import Path

import numpy as np
import pytest

from tests.test_firmware_vad import HEADER

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")

DRIVER = textwrap.dedent("""
    #include <cstdio>
    #include "raise_gesture.h"
    int main() {
      Raise::Detector d;
      unsigned ms; float ax, ay, az, gx, gy, gz;
      while (scanf("%u %f %f %f %f %f %f", &ms, &ax, &ay, &az, &gx, &gy, &gz) == 7)
        if (d.feed(ax, ay, az, gx, gy, gz, ms)) printf("%u\\n", ms);
      return 0;
    }
""")

POSE = np.array([0.0, -0.7071, -0.7071])  # the header's default
STEP_MS = 20


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    build = tmp_path_factory.mktemp("raise")
    (build / "driver.cpp").write_text(DRIVER)
    exe = build / "driver"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", f"-I{HEADER}", str(build / "driver.cpp"),
                    "-o", str(exe)], check=True)

    def feed(samples):
        text = "\n".join(f"{s[0]} " + " ".join(f"{v:.4f}" for v in s[1:]) for s in samples)
        out = subprocess.run([str(exe)], input=text, capture_output=True, text=True, check=True).stdout
        return [int(line) for line in out.split()]
    return feed


class Trace:
    """Builds a sample list: (ms, ax, ay, az, gx, gy, gz)."""

    def __init__(self, start, seed=0):
        self.g = np.array(start, dtype=float) / np.linalg.norm(start)
        self.ms = 0
        self.samples = []
        self.rng = np.random.default_rng(seed)

    def _emit(self, g, gyro_dps, jolt=0.0):
        noise = self.rng.normal(0, 0.01, 3)
        a = g * (1.0 + jolt) + noise
        gyro = np.array([gyro_dps, 0.0, 0.0]) + self.rng.normal(0, 2.0, 3)
        self.samples.append((self.ms, *a, *gyro))
        self.ms += STEP_MS

    def hold(self, ms, gyro_dps=0.0, jolt=0.0):
        for _ in range(ms // STEP_MS):
            self._emit(self.g, gyro_dps, jolt)
        return self

    def turn(self, to, ms, jolt=0.3):
        """Rotate gravity to `to` over `ms`, gyro at the matching rate, with a
        lift's acceleration bump in the middle."""
        to = np.array(to, dtype=float) / np.linalg.norm(to)
        angle = math.degrees(math.acos(np.clip(np.dot(self.g, to), -1, 1)))
        n = max(1, ms // STEP_MS)
        start = self.g
        for i in range(1, n + 1):
            t = i / n
            g = (1 - t) * start + t * to
            self.g = g / np.linalg.norm(g)
            self._emit(self.g, angle / (ms / 1000), jolt * math.sin(math.pi * t))
        return self


FLAT = [0, 0, 1]          # lying on a desk, screen up
ARM_DOWN = [0, 1, 0]      # hanging at your side
POCKET = [1, 0, 0]        # on its side


@pytest.mark.parametrize("start", [FLAT, ARM_DOWN, POCKET], ids=["desk", "wrist", "hand"])
def test_a_raise_from_each_carry_style_fires_once(run, start):
    t = Trace(start).hold(1000).turn(POSE, 500).hold(1500)
    fires = run(t.samples)
    assert len(fires) == 1
    assert 1500 + 400 <= fires[0] <= 1500 + 700  # after the 400 ms hold, not long after


def test_resting_in_the_pose_never_fires(run):
    assert run(Trace(POSE).hold(10000).samples) == []


def test_swinging_through_the_pose_does_not_fire(run):
    t = Trace(ARM_DOWN).hold(500)
    for _ in range(4):  # walking: the arm swings through the pose and back
        t.turn(POSE, 300).turn(ARM_DOWN, 300)
    assert run(t.samples) == []


def test_a_glance_too_short_to_hold_does_not_fire(run):
    t = Trace(ARM_DOWN).hold(500).turn(POSE, 400).hold(200).turn(ARM_DOWN, 400).hold(1000)
    assert run(t.samples) == []


def test_drifting_slowly_into_the_pose_does_not_fire(run):
    # 90 degrees over 6 s is 15 deg/s with no jolt: under the movement threshold.
    t = Trace(FLAT).hold(500).turn(POSE, 6000, jolt=0.0).hold(2000)
    assert run(t.samples) == []


def test_fidgeting_while_holding_it_up_does_not_fire_again(run):
    t = Trace(ARM_DOWN).hold(500).turn(POSE, 500).hold(1000)
    tilt = [0.0, -0.6, -0.8]  # a small wobble inside the pose
    for _ in range(3):
        t.turn(tilt, 300).hold(600).turn(POSE, 300).hold(600)
    assert len(run(t.samples)) == 1


def test_it_rearms_after_leaving_the_pose_and_the_cooldown(run):
    t = Trace(ARM_DOWN).hold(500).turn(POSE, 500).hold(1000).turn(ARM_DOWN, 500).hold(500)
    t.turn(POSE, 500).hold(1000)  # second raise inside the 3 s cooldown: ignored
    t.turn(ARM_DOWN, 500).hold(3000).turn(POSE, 500).hold(1000)  # after it: fires
    assert len(run(t.samples)) == 2


RECORDINGS = sorted((Path(__file__).parent / "data" / "raise").glob("*.txt"))


@pytest.mark.skipif(not RECORDINGS, reason="no recorded motion yet (tools/imu_record.py record)")
@pytest.mark.parametrize("path", RECORDINGS, ids=[p.name for p in RECORDINGS])
def test_recorded_motion(run, path):
    """Real motion from the Stick: raises fire exactly once, the rest never."""
    from tools.imu_record import read_samples
    fired = run(read_samples(path))
    assert len(fired) == (1 if path.name.startswith("raise-") else 0)
