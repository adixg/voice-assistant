"""benchmarks/tts_engines/bench_tts_engines.py -- the pure parts. The
measurement itself runs real engines and is exercised by running the tool."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "tts_engines" / "bench_tts_engines.py"
_spec = importlib.util.spec_from_file_location("bench_tts_engines", _PATH)
be = importlib.util.module_from_spec(_spec)
sys.modules["bench_tts_engines"] = be
_spec.loader.exec_module(be)


def test_parse_engine_splits_label_backend_and_flags():
    assert be.parse_engine("Kitten mini=kitten --kitten-model kitten-mini-en-v0_8") == (
        "Kitten mini", "kitten", ["--kitten-model", "kitten-mini-en-v0_8"])
    assert be.parse_engine("K=kokoro-onnx") == ("K", "kokoro-onnx", [])
    for bad in ("no-equals", "=kitten", "Label="):
        with pytest.raises(ValueError):
            be.parse_engine(bad)


def test_summarize_uses_total_audio_over_total_time():
    result = {"label": "K", "backend": "kitten", "options": {}, "load_s": 1.0, "rss_peak_mib": 300,
              "calls": [{"synth_s": 1.0, "audio_s": 4.0}, {"synth_s": 3.0, "audio_s": 4.0}]}
    s = be.summarize(result)
    assert s["x_realtime"] == 2.0 and s["s_per_10s_speech"] == 5.0
    assert s["x_realtime_min"] == pytest.approx(1.333, abs=1e-3)
    assert s["n"] == 2 and s["synth_s_max"] == 3.0


def test_shared_sentences_file_skips_comments():
    sentences = be.read_sentences(be.DEFAULT_SENTENCES)
    assert len(sentences) == 5 and not any(s.startswith("#") for s in sentences)
