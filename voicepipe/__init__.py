"""The debuggable "speech section": STT, LLM and TTS behind small, swappable
interfaces, independent of any particular entrypoint (terminal chat, M5Stick
bridge, or a one-off script).

    registry.py    the STT/LLM/TTS Protocols and the name -> backend registry
    backends/      the concrete engines; one file each, auto-discovered
    cli.py         the flags every entrypoint shares, assembled from the above
    subproc.py     shared plumbing for engines that run in their own conda env
    text.py        sentence-aware chunking, shared by every TTS backend
    personas.py    the built-in system prompts
    audio.py       local mic/speaker I/O and level metering
    cuda.py        LD_LIBRARY_PATH setup for GPU whisper

Each stage also runs on its own, which is the quickest way to find out which
one is misbehaving (see voicepipe/__main__.py):

    python -m voicepipe transcribe /tmp/hi.wav
    python -m voicepipe ask "hi there" --model rina
    python -m voicepipe speak "hello there"
"""
