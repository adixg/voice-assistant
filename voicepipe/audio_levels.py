"""How loud one utterance is, and how far above the room: for the conversation
log, and for telling speech aimed at the Stick from talk across the room.

The Stick has one microphone, so it can't separate voices by direction or by
who is speaking before STT; what it can go by is distance. On 2026-09-25 the
turns spoken to Rina had their loud frames at -2 to -12 dBFS, 20-36 dB above
the room, and the fragments the wake word caught from nearby talk sat at -13
to -25 dBFS, only 8-16 dB above it.
"""
import numpy as np

FRAME_SECONDS = 0.03


def levels(pcm, sample_rate=16000):
    """16-bit mono PCM -> {speech_dbfs, floor_dbfs, snr_db, peak_dbfs,
    clipped_pct}, or None for less than a frame. Speech is the 90th
    percentile of 30 ms frame RMS, the floor the 10th."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    frame = int(sample_rate * FRAME_SECONDS)
    count = len(samples) // frame
    if count == 0:
        return None
    rms = np.sqrt(np.mean(samples[:count * frame].reshape(count, frame) ** 2, axis=1))
    rms = np.maximum(rms, 1.0)

    def dbfs(value):
        return round(float(20 * np.log10(value / 32768.0)), 1)

    speech, floor = np.percentile(rms, 90), np.percentile(rms, 10)
    return {"speech_dbfs": dbfs(speech), "floor_dbfs": dbfs(floor),
            "snr_db": round(float(20 * np.log10(speech / floor)), 1),
            "peak_dbfs": dbfs(max(float(np.max(np.abs(samples))), 1.0)),
            "clipped_pct": round(float(np.mean(np.abs(samples) >= 32000) * 100), 2)}
