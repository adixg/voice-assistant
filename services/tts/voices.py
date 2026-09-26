"""Switching the TTS voice at runtime, between and within the sherpa-onnx
engines (voicepipe/backends/sherpa_tts.py): KittenTTS nano (8 voices, the
smallest and fastest) and Kokoro (its English voices, fuller but heavier).

Within an engine a switch only changes the speaker id and speed: instant.
Across engines the old model is closed and dropped before the new one loads
(a few seconds, and never both in memory at once), so the pod's memory limit
has to fit the bigger one, Kokoro (~1.35 GiB in the service, 2026-09-24).

The choice is saved to a small JSON file, so a restarted pod comes back with
the voice that was picked, not the manifest's.
"""
import gc
import json
import os
import tempfile
import threading

from voicepipe.backends.sherpa_tts import (KITTEN_VOICES, KOKORO_VOICES, KittenVoice, KokoroOnnxVoice,
                                           speaker_id)

# Kokoro's non-English voices would be read with its English front end, so
# only the American and British ones are offered.
KOKORO_ENGLISH = [v for v in KOKORO_VOICES if v[:2] in ("af", "am", "bf", "bm")]

# engine -> (class, voice names, default speed). Kitten nano sounds slow at
# 1.0, hence 1.6, what the cluster has run since 2026-09-24.
ENGINES = {
    "kitten": (KittenVoice, KITTEN_VOICES, 1.6),
    "kokoro": (KokoroOnnxVoice, KOKORO_ENGLISH, 1.0),
}
# The registry names of the backends this can switch between.
BACKEND_ENGINE = {"kitten": "kitten", "kokoro-onnx": "kokoro"}


class VoiceError(ValueError):
    """A voice or engine that doesn't exist, or a speed out of range."""


def resolve(engine, voice):
    """(engine, canonical voice name). `voice` may be the full name
    ("af_bella", "Luna") or, for Kokoro, the part after the prefix ("bella",
    American before British). With no engine, the first one that has it."""
    engines = [engine] if engine else list(ENGINES)
    for name in engines:
        if name not in ENGINES:
            raise VoiceError(f"unknown engine {name!r}; available: {', '.join(ENGINES)}")
        names = ENGINES[name][1]
        wanted = str(voice).strip().lower()
        for candidate in names:
            if candidate.lower() == wanted or candidate.lower().split("_", 1)[-1] == wanted:
                return name, candidate
    where = f"engine {engine}" if engine else "any engine"
    raise VoiceError(f"no voice {voice!r} in {where}")


class VoiceSwitcher:
    def __init__(self, backend, engine, voice, speed, state_file=None):
        self.backend = backend
        self.engine, self.voice, self.speed = engine, voice, speed
        self.state_file = state_file
        # synth() and switch() never overlap: a switch swaps the model under it.
        self.lock = threading.Lock()

    def current(self):
        return {"engine": self.engine, "voice": self.voice, "speed": self.speed}

    def catalog(self):
        return {"current": self.current(),
                "engines": {name: {"voices": names, "default_speed": speed}
                            for name, (_cls, names, speed) in ENGINES.items()}}

    def synth(self, text):
        with self.lock:
            return self.backend.synth(text)

    def switch(self, voice, engine=None, speed=None):
        engine, voice = resolve(engine or None, voice)
        if speed is None:
            speed = self.speed if engine == self.engine else ENGINES[engine][2]
        if not 0.5 <= float(speed) <= 2.5:
            raise VoiceError("speed must be between 0.5 and 2.5")
        with self.lock:
            cls, names, _default = ENGINES[engine]
            if engine == self.engine:
                self.backend.sid = speaker_id(voice, names)
                self.backend.speed = float(speed)
            else:
                self.backend.close()
                self.backend = None
                gc.collect()  # let the old model's memory go before the new one loads
                self.backend = cls(voice=voice, speed=float(speed))
            self.engine, self.voice, self.speed = engine, voice, float(speed)
            self._save()
        return self.current()

    def _save(self):
        if not self.state_file:
            return
        directory = os.path.dirname(self.state_file) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".voice-", dir=directory)
        with os.fdopen(fd, "w") as f:
            json.dump(self.current(), f)
        os.chmod(tmp, 0o644)  # mkstemp's 0600 would hide it from the owner on the node
        os.replace(tmp, self.state_file)

    def restore(self):
        """Apply the saved voice, if there is one and it differs."""
        if not self.state_file:
            return
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if saved == self.current():
            return
        try:
            self.switch(saved["voice"], saved["engine"], saved.get("speed"))
            print(f"  restored the saved voice: {self.current()}", flush=True)
        except (KeyError, VoiceError, ValueError) as e:
            print(f"  ! couldn't restore the saved voice {saved}: {e}", flush=True)

    def close(self):
        if self.backend is not None:
            self.backend.close()
