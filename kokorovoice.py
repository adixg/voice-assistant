from kokoro import KPipeline
import soundfile as sf
import subprocess

# Initialize TTS pipeline (American English)
pipeline = KPipeline(lang_code='b', repo_id='hexgrad/Kokoro-82M')

text = "This is a test to see which is the best voice."

# Generate audio (choose voice: 'af_bella', 'af_heart', etc.)
gen = pipeline(text, voice='bf_emma', speed=1.0)

# Save each generated sentence as WAV
for i, (gs, ps, audio) in enumerate(gen):
    sf.write(f'out_{i}.wav', audio, 24000)
    print(f"Segment {i} text: {gs}")

