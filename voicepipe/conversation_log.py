"""A durable log of every turn the gateway handled, for analysis later.

One JSON line per turn in <dir>/YYYY-MM-DD.jsonl (the time, the speaker
verdict, what STT heard, the reply, the tool calls, per-stage timings, any
error) plus that turn's recording in <dir>/audio/, named by the same id. Only
audio the Stick actually sent is here: the wake word and the endpointer run on
the Stick, and nothing leaves it until a turn starts.

Bounded: when the recordings pass `max_audio_bytes`, the oldest are deleted
(their transcript lines stay, with `audio` pointing at a file that's gone).
Like voicepipe.utterances, a failure here is printed and never breaks a turn.
"""
import json
import os
import shutil
import time

DEFAULT_MAX_AUDIO_BYTES = 2 * 1024**3  # ~2 GB: roughly 12k turns of 5 s


def new_turn():
    """The start of one turn's record; the gateway fills it in as it goes."""
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return {"id": f"{stamp}-{int(now * 1000) % 1000:03d}",
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))}


def record(directory, turn, wav_path=None, max_audio_bytes=DEFAULT_MAX_AUDIO_BYTES):
    """Append `turn` to today's log and keep `wav_path` as its recording."""
    try:
        audio_dir = os.path.join(directory, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        if wav_path and os.path.exists(wav_path):
            name = f"{turn['id']}.wav"
            shutil.copyfile(wav_path, os.path.join(audio_dir, name))
            turn["audio"] = f"audio/{name}"
        day = turn["id"][:8]
        with open(os.path.join(directory, f"{day[:4]}-{day[4:6]}-{day[6:]}.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")
        prune(audio_dir, max_audio_bytes)
    except (OSError, TypeError, ValueError) as e:
        print(f"  (couldn't log the turn: {e})", flush=True)


def prune(audio_dir, max_bytes):
    """Delete the oldest recordings until the rest fit in `max_bytes`."""
    files = sorted(f for f in os.listdir(audio_dir) if f.endswith(".wav"))
    sizes = {f: os.path.getsize(os.path.join(audio_dir, f)) for f in files}
    total = sum(sizes.values())
    for old in files:
        if total <= max_bytes:
            break
        try:
            os.unlink(os.path.join(audio_dir, old))
            total -= sizes[old]
        except OSError:
            pass


def record_event(directory, text):
    """One line from the Stick about what it decided on its own (the wake word
    fired, what started a turn, nothing heard, a near miss), in
    DIR/events-YYYY-MM-DD.jsonl next to the turns."""
    try:
        os.makedirs(directory, exist_ok=True)
        now = time.localtime()
        with open(os.path.join(directory, time.strftime("events-%Y-%m-%d.jsonl", now)), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z", now), "event": text},
                               ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  (couldn't log the event: {e})", flush=True)
