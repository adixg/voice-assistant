"""The sherpa-onnx STT/TTS backends, against a fake sherpa_onnx module, so
these run without the package or any model download."""
import argparse
import io
import os
import sys
import tarfile
import types
import wave

import numpy as np
import pytest

from voicepipe.backends import _sherpa, sherpa_stt, sherpa_tts
from voicepipe.registry import STT, TTS, STTBackend, TTSBackend


class _Stream:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.result = types.SimpleNamespace(text="")

    def accept_waveform(self, rate, samples):
        self.recognizer.received.append((rate, len(samples)))
        self.result.text = f" heard {len(samples)} samples "


class _Recognizer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.received = []

    def create_stream(self):
        return _Stream(self)

    def decode_stream(self, stream):
        pass


class _Tts:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def generate(self, text, sid=0, speed=1.0):
        self.calls.append((text, sid, speed))
        return types.SimpleNamespace(samples=[0.0, 0.5, -0.5] * 100, sample_rate=24000)


@pytest.fixture
def fake_sherpa(monkeypatch, tmp_path):
    """A fake sherpa_onnx, and a models root where every model already exists."""
    recognizer = types.SimpleNamespace(
        from_transducer=lambda **kw: _Recognizer(kind="transducer", **kw),
        from_moonshine_v2=lambda **kw: _Recognizer(kind="moonshine_v2", **kw))
    module = types.SimpleNamespace(
        OfflineRecognizer=recognizer, OfflineTts=_Tts,
        OfflineTtsConfig=lambda model: types.SimpleNamespace(model=model),
        OfflineTtsModelConfig=lambda **kw: types.SimpleNamespace(**kw),
        OfflineTtsKokoroModelConfig=lambda **kw: types.SimpleNamespace(**kw),
        OfflineTtsKittenModelConfig=lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    monkeypatch.setenv("SHERPA_MODELS_DIR", str(tmp_path))
    for name in (sherpa_stt.PARAKEET_MODEL, sherpa_stt.MOONSHINE_MODEL):
        (tmp_path / name).mkdir()
    for name, model in ((sherpa_tts.KOKORO_MODEL, "model.onnx"),
                        (sherpa_tts.KITTEN_MODEL, "model.int8.onnx")):
        (tmp_path / name).mkdir()
        (tmp_path / name / model).write_bytes(b"")
    return tmp_path


def _wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return str(path)


def test_all_four_are_registered():
    assert {"parakeet", "moonshine"} <= set(STT.names())
    assert {"kokoro-onnx", "kitten"} <= set(TTS.names())


def test_parakeet_builds_a_nemo_transducer_and_warms_up(fake_sherpa):
    stt = STT.create("parakeet", threads=2)
    kwargs = stt.recognizer.kwargs
    assert kwargs["kind"] == "transducer" and kwargs["model_type"] == "nemo_transducer"
    assert kwargs["num_threads"] == 2
    assert kwargs["encoder"].startswith(str(fake_sherpa / sherpa_stt.PARAKEET_MODEL))
    assert stt.recognizer.received == [(16000, 1600)]  # the load-time probe
    assert isinstance(stt, STTBackend)


def test_moonshine_transcribes_a_wav_and_strips_whitespace(fake_sherpa, tmp_path):
    stt = STT.create("moonshine")
    assert stt.recognizer.kwargs["kind"] == "moonshine_v2"
    path = _wav(tmp_path / "in.wav", [100, -100] * 400, rate=8000)
    assert stt.transcribe(path, "en") == "heard 800 samples"
    assert stt.recognizer.received[-1] == (8000, 800)


def test_stt_from_args(fake_sherpa):
    ap = argparse.ArgumentParser()
    STT.add_arguments(ap)
    args = ap.parse_args(["--parakeet-threads", "8"])
    assert STT.build("parakeet", args).recognizer.kwargs["num_threads"] == 8


def test_read_wav_mixes_stereo_down_and_scales_to_unit_range(tmp_path):
    path = _wav(tmp_path / "s.wav", [32767, -32767, 32767, 32767], rate=22050, channels=2)
    rate, samples = _sherpa.read_wav(path)
    assert rate == 22050
    np.testing.assert_allclose(samples, [0.0, 1.0], atol=1e-4)


def test_write_wav_round_trips_and_clips(tmp_path):
    path = str(tmp_path / "o.wav")
    _sherpa.write_wav(path, [0.0, 0.5, 2.0], 24000)
    rate, samples = _sherpa.read_wav(path)
    assert rate == 24000
    np.testing.assert_allclose(samples, [0.0, 0.5, 1.0], atol=1e-3)


@pytest.mark.parametrize("voice,expected", [("af_bella", 2), ("AF_BELLA", 2), ("3", 3), (5, 5)])
def test_kokoro_voice_by_name_or_id(voice, expected):
    assert sherpa_tts.speaker_id(voice, sherpa_tts.KOKORO_VOICES) == expected


def test_unknown_voice_lists_the_choices():
    with pytest.raises(ValueError, match="Bella"):
        sherpa_tts.speaker_id("nobody", sherpa_tts.KITTEN_VOICES)
    with pytest.raises(ValueError):
        sherpa_tts.speaker_id("99", sherpa_tts.KITTEN_VOICES)


def test_kokoro_onnx_synth_writes_one_wav_per_chunk(fake_sherpa):
    tts = TTS.create("kokoro-onnx", speed=1.1)
    assert isinstance(tts, TTSBackend)
    kokoro = tts.tts.config.model.kokoro
    assert kokoro.model.endswith("model.onnx") and kokoro.lang == "en-us"
    paths = tts.synth("First sentence. " * 40)
    assert len(paths) > 1 and all(os.path.exists(p) for p in paths)
    assert {call[1:] for call in tts.tts.calls} == {(2, 1.1)}  # af_bella, speed
    with wave.open(paths[0]) as w:
        assert w.getframerate() == 24000
    tts.close()
    assert not os.path.exists(paths[0])


def test_kitten_defaults_to_bella_and_accepts_voice_flag(fake_sherpa):
    ap = argparse.ArgumentParser()
    TTS.add_arguments(ap)
    tts = TTS.build("kitten", ap.parse_args(["--kitten-voice", "luna"]))
    assert tts.sid == 3
    assert TTS.create("kitten").sid == 1
    assert tts.tts.config.model.kitten.model.endswith("model.int8.onnx")


def test_model_dir_uses_an_existing_directory_as_is(tmp_path):
    assert _sherpa.model_dir(str(tmp_path), "asr-models") == str(tmp_path)


def test_model_dir_downloads_and_unpacks_once(monkeypatch, tmp_path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:bz2") as archive:
        data = b"tokens"
        info = tarfile.TarInfo("some-model/tokens.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    urls = []

    def fake_urlopen(url):
        urls.append(url)
        payload.seek(0)
        return payload

    monkeypatch.setattr(_sherpa.urllib.request, "urlopen", fake_urlopen)
    path = _sherpa.model_dir("some-model", "tts-models", root=str(tmp_path))
    assert path == str(tmp_path / "some-model")
    assert (tmp_path / "some-model" / "tokens.txt").read_bytes() == b"tokens"
    assert urls == [f"{_sherpa.RELEASES}/tts-models/some-model.tar.bz2"]
    assert _sherpa.model_dir("some-model", "tts-models", root=str(tmp_path)) == path
    assert len(urls) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["some-model"]  # no staging left behind


def test_model_dir_failed_download_leaves_nothing(monkeypatch, tmp_path):
    def broken(url):
        raise OSError("network down")

    monkeypatch.setattr(_sherpa.urllib.request, "urlopen", broken)
    with pytest.raises(OSError):
        _sherpa.model_dir("some-model", "asr-models", root=str(tmp_path))
    assert list(tmp_path.iterdir()) == []
