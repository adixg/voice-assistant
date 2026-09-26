"""voicepipe/audio_levels.py: loudness, room floor and clipping of one turn."""
import numpy as np

from voicepipe.audio_levels import levels


def pcm(*parts):
    return np.concatenate(parts).astype("<i2").tobytes()


def tone(amplitude, seconds, rate=16000):
    t = np.arange(int(rate * seconds)) / rate
    return amplitude * np.sin(2 * np.pi * 220 * t)


def test_speech_floor_and_snr():
    quiet, loud = tone(100, 0.5), tone(10000, 1.5)
    result = levels(pcm(quiet, loud, quiet))
    # RMS of a sine is amplitude / sqrt(2): 10000 -> -13.3 dBFS, 100 -> -53.3 dBFS.
    assert abs(result["speech_dbfs"] + 13.3) < 0.2
    assert abs(result["floor_dbfs"] + 53.3) < 0.2
    assert abs(result["snr_db"] - 40.0) < 0.3
    assert result["clipped_pct"] == 0


def test_clipping_is_counted():
    clipped = np.clip(tone(60000, 1.0), -32768, 32767)
    result = levels(pcm(clipped))
    assert result["peak_dbfs"] == 0.0 and result["clipped_pct"] > 30


def test_too_short_or_silent():
    assert levels(b"\x00\x00" * 100) is None
    assert levels(pcm(np.zeros(16000)))["speech_dbfs"] == -90.3
