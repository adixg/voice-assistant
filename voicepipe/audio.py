"""Local mic/speaker I/O via ffmpeg/ffplay (pulse). Used by chat_loop.py's
local-mic mode; bridge_server.py doesn't need this (its audio comes over a
WebSocket instead), but it's here as the shared home for the level-metering
math so both can feed the same kind of signal to a UI (e.g. the speech orb).
"""
import array
import math
import signal
import subprocess
import threading
import time
import wave


class Hooks:
    """No-op sink for UI updates; callers that don't care (headless runs,
    debug scripts) can pass nothing and record()/play() still work.

    The three signals mirror what the M5StickS3's screen shows: which state
    it's in, the live audio level driving the waveform, and the caption line
    (transcript while listening, reply while speaking)."""

    def state(self, name):
        pass

    def level(self, x):
        pass

    def caption(self, text):
        pass


def _rms16(raw, gain=8.0):
    """Normalised 0..1 loudness of a little-endian s16 mono buffer."""
    if len(raw) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(raw[: len(raw) & ~1])
    if not a:
        return 0.0
    mean_sq = sum(v * v for v in a) / len(a)
    return min(1.0, (math.sqrt(mean_sq) / 32768.0) * gain)


def record(path, hooks=None, gate=None):
    """Record the default pulse source until Enter (terminal or orb window),
    streaming the live level to `hooks` and writing a 16 kHz mono wav at `path`."""
    hooks = hooks or Hooks()
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "pulse", "-i", "default", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    buf = bytearray()

    def pump():
        while True:
            chunk = proc.stdout.read(3200)  # ~0.1 s
            if not chunk:
                break
            buf.extend(chunk)
            hooks.level(_rms16(chunk))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        if gate is None:
            input("  ● recording — Enter to stop ")
        else:
            stop = gate.get()
            if stop not in (None, ""):      # a typed command, not just Enter
                gate.push(stop)             # hand it back to the main loop
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait()
        t.join(timeout=1.0)
        hooks.level(0.0)

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(buf))
    return path


def play(path, hooks=None):
    """Play `path` with ffplay, pushing per-frame level to `hooks` in real time."""
    hooks = hooks or Hooks()
    try:
        with wave.open(path, "rb") as w:
            sr, sw = w.getframerate(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001
        raw, sr, sw = b"", 22050, 2

    proc = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path],
        stdin=subprocess.DEVNULL,
    )
    if raw and sw == 2:
        step = max(1, int(sr * 0.03)) * 2  # 30 ms of s16
        t0 = time.time()
        for off in range(0, len(raw), step):
            hooks.level(_rms16(raw[off:off + step]))
            slp = t0 + (off / 2) / sr - time.time()
            if slp > 0:
                time.sleep(slp)
            if proc.poll() is not None:
                break
    proc.wait()
    hooks.level(0.0)
