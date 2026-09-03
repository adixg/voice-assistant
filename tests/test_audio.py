"""voicepipe.audio — pure level-metering math, no ffmpeg/pulse involved."""
import array
import math

from voicepipe.audio import _rms16


def _s16_bytes(samples):
    a = array.array("h", samples)
    return a.tobytes()


class TestRms16:
    def test_empty_buffer_is_zero(self):
        assert _rms16(b"") == 0.0

    def test_single_byte_is_zero(self):
        # less than one s16 sample
        assert _rms16(b"\x01") == 0.0

    def test_silence_is_zero(self):
        assert _rms16(_s16_bytes([0] * 100)) == 0.0

    def test_odd_trailing_byte_is_ignored_not_crashed_on(self):
        # 2 full samples + 1 stray byte; must not raise
        buf = _s16_bytes([1000, -1000]) + b"\xff"
        assert _rms16(buf) >= 0.0

    def test_known_constant_amplitude_matches_formula(self):
        # constant amplitude -> rms == that amplitude exactly
        amp = 4096
        buf = _s16_bytes([amp] * 50)
        expected = min(1.0, (amp / 32768.0) * 8.0)
        assert math.isclose(_rms16(buf), expected, rel_tol=1e-6)

    def test_loud_signal_clips_to_one(self):
        buf = _s16_bytes([32767, -32768] * 50)
        assert _rms16(buf) == 1.0

    def test_custom_gain_scales_linearly(self):
        buf = _s16_bytes([1000] * 20)
        low_gain = _rms16(buf, gain=1.0)
        high_gain = _rms16(buf, gain=2.0)
        assert math.isclose(high_gain, low_gain * 2, rel_tol=1e-6)
