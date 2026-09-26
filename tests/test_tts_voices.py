"""services/tts/voices.py: resolving voice names, switching within and across
engines without ever holding two models, and remembering the choice."""
import json

import pytest

from services.tts import voices


class FakeEngine:
    made = []

    def __init__(self, voice="x", speed=1.0):
        self.voice, self.speed, self.sid, self.closed = voice, speed, None, False
        FakeEngine.made.append(self)

    def synth(self, text):
        return [f"{self.voice}:{text}"]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_engines(monkeypatch):
    FakeEngine.made = []
    monkeypatch.setitem(voices.ENGINES, "kitten", (FakeEngine, voices.KITTEN_VOICES, 1.6))
    monkeypatch.setitem(voices.ENGINES, "kokoro", (FakeEngine, voices.KOKORO_ENGLISH, 1.0))


def test_names_resolve_by_engine_and_by_short_kokoro_name():
    assert voices.resolve("kokoro", "bella") == ("kokoro", "af_bella")
    assert voices.resolve("kokoro", "EMMA") == ("kokoro", "bf_emma")
    assert voices.resolve(None, "bella") == ("kitten", "Bella")  # kitten listed first
    assert voices.resolve(None, "heart") == ("kokoro", "af_heart")
    assert "jf_alpha" not in voices.KOKORO_ENGLISH  # non-English voices aren't offered
    for engine, voice in (("kokoro", "nobody"), ("piper", "bella"), (None, "jf_alpha")):
        with pytest.raises(voices.VoiceError):
            voices.resolve(engine, voice)


def test_a_switch_within_the_engine_only_changes_the_speaker(tmp_path):
    engine = FakeEngine("Bella", 1.6)
    switcher = voices.VoiceSwitcher(engine, "kitten", "Bella", 1.6, str(tmp_path / "voice.json"))
    assert switcher.switch("luna") == {"engine": "kitten", "voice": "Luna", "speed": 1.6}
    assert switcher.backend is engine and engine.sid == voices.KITTEN_VOICES.index("Luna")
    assert len(FakeEngine.made) == 1


def test_a_switch_across_engines_closes_the_old_model_first_and_is_remembered(tmp_path):
    state = tmp_path / "voice.json"
    old = FakeEngine("Bella", 1.6)
    switcher = voices.VoiceSwitcher(old, "kitten", "Bella", 1.6, str(state))
    assert switcher.switch("bella", engine="kokoro") == {"engine": "kokoro", "voice": "af_bella", "speed": 1.0}
    assert old.closed and switcher.backend is not old
    assert switcher.synth("hi") == ["af_bella:hi"]
    assert json.loads(state.read_text()) == {"engine": "kokoro", "voice": "af_bella", "speed": 1.0}
    assert state.stat().st_mode & 0o777 == 0o644

    # A restarted pod comes back to it.
    fresh = voices.VoiceSwitcher(FakeEngine("Bella", 1.6), "kitten", "Bella", 1.6, str(state))
    fresh.restore()
    assert fresh.current() == {"engine": "kokoro", "voice": "af_bella", "speed": 1.0}
    with pytest.raises(voices.VoiceError):
        fresh.switch("bella", engine="kokoro", speed=9)


def test_the_voice_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    import services.tts.app as tts_app
    switcher = voices.VoiceSwitcher(FakeEngine("Bella", 1.6), "kitten", "Bella", 1.6)
    monkeypatch.setattr(tts_app, "_voices", switcher)
    monkeypatch.setattr(tts_app, "_backend", switcher.backend)
    client = TestClient(tts_app.app)
    assert client.get("/voices").json()["current"]["voice"] == "Bella"
    assert client.post("/voice", json={"voice": "heart", "engine": "kokoro"}).json()["voice"] == "af_heart"
    assert client.post("/voice", json={"voice": "nobody"}).status_code == 422
    assert client.get("/health").json()["voice"]["voice"] == "af_heart"
    monkeypatch.setattr(tts_app, "_voices", None)
    assert client.get("/voices").status_code == 409
