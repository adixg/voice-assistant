"""firmware/m5stick_bridge/src/vad.h, compiled and run on the host with g++.

The endpointer is plain C++ precisely so its decisions can be checked here
rather than only by talking to the Stick. Loudness sequences are in raw RMS
units per 32 ms chunk: ~60 is a quiet room, ~2000 is speech at arm's length.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HEADER = ROOT / "firmware" / "m5stick_bridge" / "src"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")

DRIVER = textwrap.dedent("""
    #include <cstdio>
    #include <vector>
    #include "vad.h"
    int main() {
      Vad::Endpointer ep;
      float level;
      const char *names[] = {"WAITING","SPEECH_START","CONTINUE","END","NO_SPEECH","MAX_LENGTH"};
      while (scanf("%f", &level) == 1) {
        Vad::Decision d = ep.feedRms(level);
        printf("%s\\n", names[d]);
        if (d == Vad::END || d == Vad::NO_SPEECH || d == Vad::MAX_LENGTH) break;
      }
      return 0;
    }
""")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    build = tmp_path_factory.mktemp("vad")
    (build / "driver.cpp").write_text(DRIVER)
    exe = build / "driver"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", f"-I{HEADER}", str(build / "driver.cpp"),
                    "-o", str(exe)], check=True)

    def feed(levels):
        out = subprocess.run([str(exe)], input=" ".join(map(str, levels)), capture_output=True,
                             text=True, check=True).stdout.split()
        return out
    return feed


QUIET, SPEECH = 60, 2000
CHUNK_MS = 32


def chunks(ms):
    return -(-ms // CHUNK_MS)  # ceil


def test_speech_then_silence_ends_after_the_hangover(run):
    levels = [QUIET] * 10 + [SPEECH] * 30 + [QUIET] * 40
    out = run(levels)
    assert out.count("SPEECH_START") == 1
    assert out[-1] == "END"
    quiet_before_end = len(out) - (10 + 30)
    assert quiet_before_end == chunks(800)  # the 800 ms hangover, no earlier


def test_a_short_pause_mid_sentence_does_not_end_the_turn(run):
    pause = [QUIET] * chunks(500)  # "what's the weather ... tomorrow"
    out = run([QUIET] * 10 + [SPEECH] * 20 + pause + [SPEECH] * 20 + [QUIET] * 40)
    assert "END" not in out[: 10 + 20 + len(pause) + 20]
    assert out[-1] == "END"


def test_silence_alone_cancels_without_ever_starting(run):
    out = run([QUIET] * 400)
    assert "SPEECH_START" not in out
    assert out[-1] == "NO_SPEECH" and len(out) == chunks(5000)


def test_a_click_is_not_speech(run):
    # one loud chunk (a button click, a tap on the case) must not start a turn
    out = run([QUIET] * 10 + [SPEECH] + [QUIET] * 400)
    assert "SPEECH_START" not in out and out[-1] == "NO_SPEECH"


def test_a_noisy_room_raises_the_bar(run):
    # background at 800: "speech" at 2000 is only 2.5x that, below the 3.2x ratio
    out = run([800] * 400)
    assert "SPEECH_START" not in out
    out = run([800] * 10 + [3000] * 20 + [800] * 40)  # clearly above it still counts
    assert "SPEECH_START" in out and out[-1] == "END"


def test_endless_noise_after_speech_hits_the_cap(run):
    out = run([QUIET] * 10 + [SPEECH] * 1000)
    assert out[-1] == "MAX_LENGTH" and len(out) == chunks(20000)


def test_speech_right_at_the_start_is_caught(run):
    # the calibration window is short: someone who starts talking immediately
    # still triggers once the floor is learned from those first chunks
    out = run([QUIET] * 4 + [SPEECH] * 30 + [QUIET] * 40)
    assert "SPEECH_START" in out and out[-1] == "END"
