"""Speaker verification with WeSpeaker's ECAPA-TDNN-512.

Turns a wav into a 192-dimensional embedding; two embeddings of the same
person score high against each other and low against anyone else. That is what
lets the bridge answer only to one voice.

Runs in-process in the `chat` env rather than shelling out to its own conda
env like the TTS backends do: the ONNX model is only 24 MB, it needs
onnxruntime and kaldi-native-fbank (neither of which pulls torch), and it sits
in the path of *every* utterance — a worker handshake per turn would cost more
than the inference does.

Two details the model is unforgiving about, both learned the hard way:

* **16 kHz or nothing.** ECAPA was trained at 16 kHz; features computed at any
  other rate put the mel bins on the wrong frequencies and the embeddings stop
  discriminating. Measured on a controlled pair (two VITS speaker ids, same
  sentences): at native 24 kHz, same-speaker scored *below* cross-speaker;
  after resampling, same-speaker 0.67-0.77 vs cross-speaker 0.34-0.51.
* **Raw int16 scale.** Kaldi fbank expects samples in int16 range, not the
  [-1, 1] floats a normal audio library hands back.
"""
import os
import subprocess
import tempfile
import wave

import numpy as np

from ..registry import SV

SAMPLE_RATE = 16000
EMBEDDING_DIM = 192
NUM_MEL_BINS = 80

# The official WeSpeaker ECAPA-512 export, trained on VoxCeleb2.
MODEL_REPO = "Wespeaker/wespeaker-ecapa-tdnn512-LM"
MODEL_FILE = "voxceleb_ECAPA512_LM.onnx"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{MODEL_FILE}"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "voicepipe")


def default_model_path():
    return os.path.join(CACHE_DIR, MODEL_FILE)


def ensure_model(path=None):
    """The ONNX model on disk, downloading it once if it isn't there."""
    path = path or default_model_path()
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"  downloading {MODEL_FILE} (~24MB) to {path} ...", flush=True)
    partial = path + ".part"  # never leave a truncated file that looks complete
    try:
        subprocess.run(["curl", "-sSfL", "-o", partial, MODEL_URL], check=True)
        os.replace(partial, path)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(partial)
        raise RuntimeError(f"couldn't download the speaker model from {MODEL_URL}: {e}") from e
    return path


def read_wav_16k(path):
    """Mono int16-scale samples at 16 kHz, resampling only when needed.

    The bridge already records at 16 kHz, so the common path does no work; an
    enrollment clip from anywhere else gets converted.
    """
    with wave.open(path, "rb") as w:
        if w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2:
            raw = w.readframes(w.getnframes())
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    converted = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path,
             "-ac", "1", "-ar", str(SAMPLE_RATE), converted],
            check=True)
        with wave.open(converted, "rb") as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    finally:
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(converted)


def fbank(samples):
    """80-bin Kaldi fbank with cepstral mean normalisation, as WeSpeaker's own
    front-end computes it."""
    import kaldi_native_fbank as knf

    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = float(SAMPLE_RATE)
    opts.frame_opts.dither = 0.0        # deterministic: the same clip must embed identically
    opts.frame_opts.snip_edges = False
    opts.mel_opts.num_bins = NUM_MEL_BINS

    extractor = knf.OnlineFbank(opts)
    extractor.accept_waveform(SAMPLE_RATE, samples.tolist())
    extractor.input_finished()
    if extractor.num_frames_ready == 0:
        raise ValueError("clip too short to produce any frames")
    feats = np.stack([extractor.get_frame(i) for i in range(extractor.num_frames_ready)])
    return feats - feats.mean(axis=0, keepdims=True)


@SV.register("wespeaker")
class WeSpeakerSV:
    """SpeakerBackend: wav -> unit-length 192-d embedding."""

    name = "wespeaker-ecapa-tdnn512"
    dimensions = EMBEDDING_DIM

    # Both measured on this model, on real voices through the Stick's mic:
    # the owner scored 0.765-0.895 against his own print and a different
    # person 0.069-0.075, so 0.6 sits in a wide gap.
    default_threshold = 0.6
    # And by truncating a known 10.6s clip: 0.75s -> 0.074, 1.5s -> 0.617,
    # 2.0s -> 0.829. Below two seconds the same person scores like a stranger.
    min_verify_seconds = 2.0

    def __init__(self, model=None, session=None):
        self.model_path = model or default_model_path()
        self._session = session

    @staticmethod
    def add_arguments(group):
        group.add_argument("--speaker-model", default=None, metavar="ECAPA.ONNX",
                           help=f"WeSpeaker ECAPA ONNX model (default: {default_model_path()}, "
                                f"downloaded on first use)")

    @classmethod
    def from_args(cls, args):
        return cls(model=args.speaker_model)

    @property
    def session(self):
        """Loaded lazily so importing this module costs nothing and the model
        is only fetched when speaker checking is actually switched on."""
        if self._session is None:
            import onnxruntime as ort
            self._session = ort.InferenceSession(ensure_model(self.model_path),
                                                 providers=["CPUExecutionProvider"])
        return self._session

    def embed(self, wav_path):
        """A unit-length embedding, so a dot product is the cosine score."""
        feats = fbank(read_wav_16k(wav_path))[None, :, :].astype(np.float32)
        vector = self.session.run(["embs"], {"feats": feats})[0][0]
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("model produced a zero embedding")
        return vector / norm
