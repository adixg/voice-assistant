"""The Stick's wire audio format: 16 kHz mono PCM16, both directions.

Shared by bridge_server.py and services/gateway/app.py so the resample/
normalize step -- and its tuning -- can't drift between the monolith and the
split gateway that's meant to become its drop-in replacement.
"""
import asyncio

SAMPLE_RATE = 16000
SEND_CHUNK = 4000

# ffmpeg's one-pass speech normalizer. The Stick's speaker is already at
# M5.Speaker.setVolume(255) -- full scale -- but the TTS doesn't use the range
# it is given: a measured VITS reply peaked at -3.5 dB with a -18.1 dB mean,
# so the amplifier was at 100% driving a signal at roughly two thirds. This
# filter lifts it to about -0.4 dB peak / -14.7 dB mean without clipping, and
# evens out quiet syllables rather than applying flat gain (which would clip
# the loud chunks instead). Measured at 153x realtime, so it costs nothing
# against a turn that already spends a second in TTS.
NORMALIZE_FILTER = "speechnorm=e=6.25:r=0.00001:l=1"


async def resample_to_pcm16(wav_path, normalize=True):
    """ffmpeg any wav to raw 16kHz mono s16le bytes, for the Stick's I2S speaker."""
    filters = ["-af", NORMALIZE_FILTER] if normalize else []
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", wav_path, *filters, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    data, _ = await proc.communicate()
    return data
