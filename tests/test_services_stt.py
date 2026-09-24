"""services/stt/app.py -- the STT registry served over HTTP. Backends are
swapped for a fake at the module level, the same seam services/stt/app.py's
own `main()` uses, so these tests exercise the real FastAPI wiring without a
real faster-whisper model."""
import os

from fastapi.testclient import TestClient

import services.stt.app as stt_app


class FakeSTT:
    def __init__(self, reply="hello there"):
        self.reply = reply
        self.calls = []

    def transcribe(self, wav_path, lang):
        self.calls.append((wav_path, lang))
        return self.reply


class TestHealth:
    def test_reports_ok_with_no_backend(self, monkeypatch):
        monkeypatch.setattr(stt_app, "_backend", None)
        client = TestClient(stt_app.app)

        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "backend": None}

    def test_reports_the_backend_class_name(self, monkeypatch):
        monkeypatch.setattr(stt_app, "_backend", FakeSTT())
        client = TestClient(stt_app.app)

        assert client.get("/health").json()["backend"] == "FakeSTT"


class TestTranscribe:
    def test_returns_503_when_backend_not_ready(self, monkeypatch):
        monkeypatch.setattr(stt_app, "_backend", None)
        client = TestClient(stt_app.app)

        resp = client.post("/transcribe", files={"audio": ("t.wav", b"RIFF....", "audio/wav")})

        assert resp.status_code == 503

    def test_writes_the_upload_to_a_temp_wav_and_returns_the_transcript(self, monkeypatch):
        fake = FakeSTT(reply="turn the lights on")
        monkeypatch.setattr(stt_app, "_backend", fake)
        client = TestClient(stt_app.app)

        resp = client.post("/transcribe",
                            files={"audio": ("t.wav", b"RIFF....fakewavbytes", "audio/wav")},
                            params={"lang": "en"})

        assert resp.status_code == 200
        assert resp.json() == {"text": "turn the lights on"}
        assert len(fake.calls) == 1
        path, lang = fake.calls[0]
        assert path.endswith(".wav")
        assert lang == "en"

    def test_lang_defaults_to_none_for_auto_detect(self, monkeypatch):
        fake = FakeSTT()
        monkeypatch.setattr(stt_app, "_backend", fake)
        client = TestClient(stt_app.app)

        client.post("/transcribe", files={"audio": ("t.wav", b"data", "audio/wav")})

        assert fake.calls[0][1] is None

    def test_deletes_the_temp_file_after_transcribing(self, monkeypatch):
        seen_path = {}

        class RecordingSTT(FakeSTT):
            def transcribe(self, wav_path, lang):
                seen_path["path"] = wav_path
                assert os.path.exists(wav_path)
                return super().transcribe(wav_path, lang)

        monkeypatch.setattr(stt_app, "_backend", RecordingSTT())
        client = TestClient(stt_app.app)

        client.post("/transcribe", files={"audio": ("t.wav", b"data", "audio/wav")})

        assert not os.path.exists(seen_path["path"])


def test_health_reports_backend_config_when_set(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(stt_app, "_backend", FakeSTT())
    monkeypatch.setattr(stt_app, "_config", {"name": "moonshine", "options": {"moonshine_threads": 4}})
    body = TestClient(stt_app.app).get("/health").json()
    assert body["config"] == {"name": "moonshine", "options": {"moonshine_threads": 4}}
