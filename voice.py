import os
import torch
from openvoice import se_extractor
from openvoice.api import BaseSpeakerTTS, ToneColorConverter
import subprocess
import warnings

warnings.filterwarnings("ignore")

def main(text):
    ckpt_base = '/home/aditya/github/OpenVoice/checkpoints/base_speakers/EN'
    ckpt_converter = '/home/aditya/github/OpenVoice/checkpoints/converter'
    device="cuda:0" if torch.cuda.is_available() else "cpu"
    output_dir = '/home/aditya/outputs'
    
    base_speaker_tts = BaseSpeakerTTS(f'{ckpt_base}/config.json', device=device)
    base_speaker_tts.load_ckpt(f'{ckpt_base}/checkpoint.pth')
    
    tone_color_converter = ToneColorConverter(f'{ckpt_converter}/config.json', device=device)
    tone_color_converter.load_ckpt(f'{ckpt_converter}/checkpoint.pth')
    
    os.makedirs(output_dir, exist_ok=True)
    
    source_se = torch.load(f'{ckpt_base}/en_default_se.pth').to(device)
    
    reference_speaker = '/home/aditya/github/OpenVoice/resources/demo_speaker1.mp3' # This is the voice you want to clone
    #reference_speaker = '/home/aditya/aigf/rinavoice.mp3'
    target_se, audio_name = se_extractor.get_se(reference_speaker, tone_color_converter, target_dir='processed', vad=True)
    
    save_path = f'{output_dir}/output_en_default.wav'
    
    # Run the base speaker tts
    #text = "This is a test audio to see if its working."
    src_path = f'{output_dir}/tmp.wav'
    base_speaker_tts.tts(text, src_path, speaker='default', language='English', speed=1.0)
    
    # Run the tone color converter
    encode_message = "@MyShell"
    tone_color_converter.convert(
        audio_src_path=src_path, 
        src_se=source_se, 
        tgt_se=target_se, 
        output_path=save_path,
        message=encode_message)
    
    subprocess.run(["ffplay", "-nodisp", "-autoexit", save_path])

while True:
    a = input("Speak: ")
    main(a)

