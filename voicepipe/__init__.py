"""The debuggable "speech section": STT, LLM, and TTS as plain importable
modules, independent of any particular entrypoint (terminal chat, M5Stick
bridge, or a one-off debug script).

    from voicepipe import stt, llm, tts, audio

Each module can be exercised on its own, e.g.:

    python -m voicepipe.tts "hello there" -o /tmp/hi.wav
    python -m voicepipe.stt /tmp/hi.wav
"""
