"""services/tts/app.py -- the TTS registry served over HTTP. `synth()`
returns a list of wav chunk paths; the service reads and base64-encodes
each, in order, which is what these tests check."""
import base64
import tempfile
from unittest.mock import Mock

from fastapi.testclient import TestClient

import services.tts.app as tts_app
from services.tts.elevenlabs import ElevenLabsTTS


class FakeTTS:
    def __init__(self, chunk_bytes=None):
        self.chunk_bytes = chunk_bytes if chunk_bytes is not None else [b"chunk-one", b"chunk-two"]
        self.closed = False
        self.requested_text = None

    def synth(self, text):
        self.requested_text = text
        paths = []
        for data in self.chunk_bytes:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(data)
                paths.append(f.name)
        return paths

    def close(self):
        self.closed = True


class TestHealth:
    def test_reports_ok_with_no_backend(self, monkeypatch):
        monkeypatch.setattr(tts_app, "_backend", None)
        client = TestClient(tts_app.app)

        assert client.get("/health").json() == {"status": "ok", "backend": None}


class TestSynth:
    def test_returns_503_when_backend_not_ready(self, monkeypatch):
        monkeypatch.setattr(tts_app, "_backend", None)
        client = TestClient(tts_app.app)

        resp = client.post("/synth", json={"text": "hi"})

        assert resp.status_code == 503

    def test_returns_each_chunk_base64_encoded_in_order(self, monkeypatch):
        fake = FakeTTS(chunk_bytes=[b"first", b"second", b"third"])
        monkeypatch.setattr(tts_app, "_backend", fake)
        client = TestClient(tts_app.app)

        resp = client.post("/synth", json={"text": "read this back to me"})

        assert resp.status_code == 200
        chunks = resp.json()["chunks_b64"]
        assert [base64.b64decode(c) for c in chunks] == [b"first", b"second", b"third"]
        assert fake.requested_text == "read this back to me"

    def test_empty_chunk_list_is_a_valid_empty_reply(self, monkeypatch):
        monkeypatch.setattr(tts_app, "_backend", FakeTTS(chunk_bytes=[]))
        client = TestClient(tts_app.app)

        resp = client.post("/synth", json={"text": ""})

        assert resp.json()["chunks_b64"] == []


class TestElevenLabs:
    def test_wraps_pcm_response_as_wav(self, tmp_path):
        backend = ElevenLabsTTS("voice", "key")
        backend.client.close()
        backend.client = Mock()
        backend.client.post.return_value = Mock(content=b"\x00\x00" * 8)
        backend.client.post.return_value.raise_for_status = Mock()

        [path] = backend.synth("hello")
        with open(path, "rb") as audio:
            assert audio.read(4) == b"RIFF"
        backend.close()
