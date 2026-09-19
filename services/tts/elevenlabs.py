"""Small ElevenLabs TTS backend using the provider's raw PCM output."""

import os
import tempfile
import wave

import httpx
from prometheus_client import Counter, Gauge


ELEVENLABS_CHARACTERS = Counter(
    "elevenlabs_characters_total",
    "Characters successfully sent to ElevenLabs; approximately one credit per character",
    ("model",),
)
ELEVENLABS_CREDITS_REMAINING = Gauge(
    "elevenlabs_credits_remaining_estimate",
    "Estimated ElevenLabs credits remaining, assuming one credit per character",
    ("model",),
)


class ElevenLabsTTS:
    """Synthesize 16 kHz mono PCM and expose it as a temporary WAV chunk."""

    def __init__(self, voice_id, api_key, model_id="eleven_multilingual_v2", credits_remaining=None):
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required")
        if not voice_id:
            raise ValueError("ELEVENLABS_VOICE_ID is required")
        self.voice_id = voice_id
        self.model_id = model_id
        self.credits_remaining = credits_remaining
        if credits_remaining is not None:
            ELEVENLABS_CREDITS_REMAINING.labels(model_id).set(credits_remaining)
        self.client = httpx.Client(
            base_url="https://api.elevenlabs.io",
            headers={"xi-api-key": api_key, "content-type": "application/json"},
            timeout=60.0,
        )

    @classmethod
    def from_environment(cls):
        return cls(
            voice_id=os.environ.get("ELEVENLABS_VOICE_ID"),
            api_key=os.environ.get("ELEVENLABS_API_KEY"),
            model_id=os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
            credits_remaining=float(os.environ["ELEVENLABS_CREDITS_REMAINING"])
            if os.environ.get("ELEVENLABS_CREDITS_REMAINING") else None,
        )

    def synth(self, text):
        response = self.client.post(
            f"/v1/text-to-speech/{self.voice_id}",
            params={"output_format": "pcm_16000"},
            json={"text": text, "model_id": self.model_id},
        )
        response.raise_for_status()
        characters = len(text)
        ELEVENLABS_CHARACTERS.labels(self.model_id).inc(characters)
        if self.credits_remaining is not None:
            self.credits_remaining = max(0, self.credits_remaining - characters)
            ELEVENLABS_CREDITS_REMAINING.labels(self.model_id).set(self.credits_remaining)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
            path = output.name
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(response.content)
        return [path]

    def close(self):
        self.client.close()
