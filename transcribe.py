import tkinter as tk
import subprocess
import whisper
import threading
import output

recording_process = None

def start_recording():
    global recording_process
    filename = "recorded_audio.wav"
    command = ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", "10", filename]
    recording_process = subprocess.Popen(command)
    button.config(text="Stop Recording", command=stop_recording)

def stop_recording():
    global recording_process
    if recording_process:
        recording_process.terminate()
        recording_process.wait()
    transcribe_audio()
    button.config(text="Start Recording", command=start_recording)

def transcribe_audio():
    model = whisper.load_model("small")
    result = model.transcribe("recorded_audio.wav")
    text_output.config(state=tk.NORMAL)
    text_output.delete("1.0", tk.END)
    text_output.insert(tk.END, result["text"])
    text_output.config(state=tk.DISABLED)
    output.main(result["text"])

# Create GUI window
root = tk.Tk()
root.title("Whisper Speech-to-Text")

button = tk.Button(root, text="Start Recording", command=start_recording, font=("Arial", 14))
button.pack(pady=20)

text_output = tk.Text(root, height=10, width=50, font=("Arial", 12))
text_output.pack()
text_output.config(state=tk.DISABLED)

root.mainloop()

