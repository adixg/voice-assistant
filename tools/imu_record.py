#!/usr/bin/env python3
"""Record real motion from the Stick and fit the raise-to-talk detector to it.

    # 1. flash the logger:   cd firmware/imu_logger && pio run -t upload
    python tools/imu_record.py record            # guided session -> tests/data/raise/
    python tools/imu_record.py fit               # the pose, from the raises
    python tools/imu_record.py replay [--pose X Y Z --cone DEG]
    # 2. put the fitted pose/cone into raise_gesture.h's Config, flash m5stick_bridge back

Recordings are "ms ax ay az gx gy gz" lines (g, deg/s) from
firmware/imu_logger, named <label>-<n>.txt: files starting "raise-" must make
the detector fire, every other one must not. tests/test_firmware_raise.py
replays them all against the header's defaults, so they keep guarding the
thresholds after they're fitted.
"""
import argparse
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data" / "raise"
HEADER_DIR = ROOT / "firmware" / "m5stick_bridge" / "src"

# (label, instruction, seconds per take, takes). The raises cover the three
# carry styles; the rest is everyday motion that must not fire.
SESSION = [
    ("raise-desk", "Stick flat on the desk. On GO: pick it up, look at the screen for a second, put it back.", 5, 5),
    ("raise-hand", "Stick in your hand at your side (or in a pocket). On GO: bring it up and look at it for a second, lower it.", 5, 5),
    ("raise-wrist", "Stick on your wrist, arm down. On GO: raise your wrist to look at it for a second, lower it.", 5, 5),
    ("walk", "Carry it the way you usually do and walk around.", 15, 1),
    ("type", "Wrist/hand on the keyboard: type normally.", 15, 1),
    ("pickup-no-look", "On GO: pick it up and put it down somewhere else without looking at it.", 5, 3),
    ("pocket", "On GO: put it in a pocket, then take it out again.", 8, 2),
    ("fidget", "Hold it up looking at it, and tilt/turn it around a bit the whole time.", 10, 1),
    ("gesture", "Talk with your hands: wave, point, reach for things.", 15, 1),
]

DRIVER = """
#include <cstdio>
#include <cstdlib>
#include "raise_gesture.h"
int main(int argc, char **argv) {
  Raise::Config c;
  if (argc >= 5) { c.pose[0] = atof(argv[1]); c.pose[1] = atof(argv[2]); c.pose[2] = atof(argv[3]);
                   c.poseConeDeg = atof(argv[4]); }
  Raise::Detector d(c);
  unsigned ms; float ax, ay, az, gx, gy, gz;
  while (scanf("%u %f %f %f %f %f %f", &ms, &ax, &ay, &az, &gx, &gy, &gz) == 7)
    if (d.feed(ax, ay, az, gx, gy, gz, ms)) printf("%u\\n", ms);
  return 0;
}
"""


def read_samples(path):
    samples = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 7 and not line.startswith("#"):
            try:
                samples.append((int(parts[0]), *map(float, parts[1:])))
            except ValueError:
                continue
    return samples


def build_driver(workdir):
    if shutil.which("g++") is None:
        sys.exit("needs g++")
    src, exe = Path(workdir) / "driver.cpp", Path(workdir) / "driver"
    src.write_text(DRIVER)
    subprocess.run(["g++", "-std=c++17", "-O2", f"-I{HEADER_DIR}", str(src), "-o", str(exe)], check=True)
    return exe


def fires(exe, path, pose=None, cone=None):
    args = [str(exe)] + ([*map(str, pose), str(cone)] if pose is not None else [])
    text = "\n".join(" ".join(map(str, s)) for s in read_samples(path))
    out = subprocess.run(args, input=text, capture_output=True, text=True, check=True).stdout
    return [int(x) for x in out.split()]


def record(args):
    import serial

    DATA.mkdir(parents=True, exist_ok=True)
    port = serial.Serial(args.port, 115200, timeout=0.2)
    time.sleep(1.0)
    print("Recording to", DATA)
    for label, instruction, seconds, takes in SESSION:
        print(f"\n== {label}: {instruction}")
        for take in range(1, takes + 1):
            path = DATA / f"{label}-{take}.txt"
            if path.exists() and not args.overwrite:
                print(f"   {path.name} exists, skipping")
                continue
            input(f"   take {take}/{takes}: press Enter, then GO when you see it... ")
            for n in (3, 2, 1):
                print(f"   {n}", flush=True)
                time.sleep(0.7)
            port.reset_input_buffer()
            print(f"   GO  ({seconds} s)", flush=True)
            lines, t0, end = [], None, time.time() + seconds
            while time.time() < end:
                parts = port.readline().decode("ascii", "replace").split()
                if len(parts) != 7:
                    continue
                try:
                    ms = int(parts[0])
                except ValueError:
                    continue
                t0 = ms if t0 is None else t0
                lines.append(" ".join([str(ms - t0), *parts[1:]]))
            path.write_text("\n".join(lines) + "\n")
            print(f"   saved {path.name} ({len(lines)} samples)")


def fit(args):
    """The pose is the mean gravity direction over each raise's steadiest
    ~0.4 s window after its biggest movement; the cone covers the spread."""
    finals = []
    for path in sorted(DATA.glob("raise-*.txt")):
        s = read_samples(path)
        if len(s) < 30:
            continue
        gyro = [math.sqrt(x[4] ** 2 + x[5] ** 2 + x[6] ** 2) for x in s]
        peak = max(range(len(s)), key=lambda i: gyro[i])
        best, best_i = None, None
        for i in range(peak, len(s) - 20):
            score = max(gyro[i:i + 20])
            if best is None or score < best:
                best, best_i = score, i
        if best_i is None:
            continue
        window = s[best_i:best_i + 20]
        g = [sum(x[k] for x in window) / len(window) for k in (1, 2, 3)]
        n = math.sqrt(sum(v * v for v in g))
        finals.append((path.name, [v / n for v in g], best))
    if not finals:
        sys.exit(f"no raise-*.txt recordings in {DATA}")
    pose = [sum(f[1][k] for f in finals) / len(finals) for k in range(3)]
    n = math.sqrt(sum(v * v for v in pose))
    pose = [v / n for v in pose]
    spread = []
    for name, g, still in finals:
        angle = math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(g, pose))))))
        spread.append(angle)
        print(f"  {name:18} gravity {g[0]:+.2f} {g[1]:+.2f} {g[2]:+.2f}  {angle:5.1f} deg off  "
              f"(steadiest gyro {still:.0f} deg/s)")
    cone = min(60.0, max(20.0, max(spread) + 10.0))
    print(f"\npose = {{{pose[0]:.3f}f, {pose[1]:.3f}f, {pose[2]:.3f}f}}, poseConeDeg = {cone:.0f}")
    print(f"replay: python tools/imu_record.py replay --pose {pose[0]:.3f} {pose[1]:.3f} {pose[2]:.3f} "
          f"--cone {cone:.0f}")


def replay(args):
    with tempfile.TemporaryDirectory() as work:
        exe = build_driver(work)
        ok = True
        for path in sorted(DATA.glob("*.txt")):
            got = fires(exe, path, args.pose, args.cone)
            want_fire = path.name.startswith("raise-")
            good = bool(got) == want_fire and (not want_fire or len(got) == 1)
            ok &= good
            print(f"  {'ok  ' if good else 'FAIL'} {path.name:20} fired {len(got)}x {got}")
        print("all good" if ok else "some recordings disagree with the detector")
        return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--port", default="/dev/ttyACM0")
    r.add_argument("--overwrite", action="store_true")
    sub.add_parser("fit")
    p = sub.add_parser("replay")
    p.add_argument("--pose", type=float, nargs=3)
    p.add_argument("--cone", type=float, default=35.0)
    args = ap.parse_args()
    return {"record": record, "fit": fit, "replay": replay}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
