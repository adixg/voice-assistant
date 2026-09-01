from kokoro import KPipeline
import soundfile as sf
import subprocess
import threading
import tempfile

def play_audio(waveform):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpfile:
        sf.write(tmpfile.name, waveform, 24000)
        subprocess.run(["ffplay", "-nodisp", "-autoexit", tmpfile.name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
text = "This is a test to see which is the best voice."

#voices = ['bf_alice','bf_emma','bf_isabella','bf_lily']
#voices = ['af_heart','af_alloy','af_aoede','af_bella','af_jessica','af_kore','af_nicole','af_nova','af_river','af_sarah','af_sky']
voices = ['af_heart', 'af_bella']

for x in voices:
    gen = pipeline(text, voice=x, speed=1.0)

    for i, (gs, ps, audio) in enumerate(gen):
        #print(f"Segment {i} text: {gs}")
        print(x)
        thread = threading.Thread(target=play_audio, args=(audio,))
        thread.start()
        thread.join()
