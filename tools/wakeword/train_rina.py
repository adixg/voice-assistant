"""Augment the generated clips, build features, and train the "Rina-chan" model.

Follows kahrendt/microWakeWord's basic_training_notebook.ipynb, with two
additions: the similar-sounding negatives in adversarial/ are a feature set of
their own (truth False), and training runs 20k steps instead of 10k.

    python train_rina.py features   # augment + spectrograms (CPU)
    python train_rina.py train      # train + quantize + streaming export (GPU)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
os.chdir(ROOT)


def augmenter():
    from microwakeword.audio.augmentation import Augmentation
    return Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.1, "TanhDistortion": 0.1, "PitchShift": 0.1,
            "BandStopFilter": 0.1, "AddColorNoise": 0.1, "AddBackgroundNoise": 0.75,
            "Gain": 1.0, "RIR": 0.5,
        },
        impulse_paths=["mit_rirs"],
        background_paths=["fma_16k", "audioset_16k"],
        background_min_snr_db=-5,
        background_max_snr_db=10,
        min_jitter_s=0.195,
        max_jitter_s=0.205,
    )


def features(clip_dir, out_root):
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration
    from mmap_ninja.ragged import RaggedMmap

    clips = Clips(input_directory=clip_dir, file_pattern="*.wav", max_clip_duration_s=None,
                  remove_silence=False, random_split_seed=10, split_count=0.1)
    aug = augmenter()
    for split, name, repeat, slide in [("training", "train", 2, 10), ("validation", "validation", 1, 10),
                                       ("testing", "test", 1, 1)]:
        out = Path(out_root, split, "wakeword_mmap")
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        spectrograms = SpectrogramGeneration(clips=clips, augmenter=aug, slide_frames=slide, step_ms=10)
        RaggedMmap.from_generator(out_dir=str(out), batch_size=100, verbose=True,
                                  sample_generator=spectrograms.spectrogram_generator(split=name, repeat=repeat))


def config():
    def mmap(path, weight, truth, strategy):
        return {"features_dir": path, "sampling_weight": weight, "penalty_weight": 1.0,
                "truth": truth, "truncation_strategy": strategy, "type": "mmap"}
    cfg = {
        "window_step_ms": 10,
        "train_dir": "trained_models/rina_chan",
        "features": [
            mmap("generated_augmented_features", 2.0, True, "truncate_start"),
            mmap("adversarial_augmented_features", 3.0, False, "truncate_start"),
            mmap("negative_datasets/speech", 10.0, False, "random"),
            mmap("negative_datasets/dinner_party", 10.0, False, "random"),
            mmap("negative_datasets/no_speech", 5.0, False, "random"),
            mmap("negative_datasets/dinner_party_eval", 0.0, False, "split"),
        ],
        "training_steps": [int(os.environ.get("MWW_STEPS", "20000"))],
        "positive_class_weight": [1],
        "negative_class_weight": [20],
        "learning_rates": [0.001],
        "batch_size": 128,
        "time_mask_max_size": [0], "time_mask_count": [0],
        "freq_mask_max_size": [0], "freq_mask_count": [0],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "target_minimization": 0.9,
        "minimization_metric": None,
        "maximization_metric": "average_viable_recall",
    }
    Path("training_parameters.yaml").write_text(yaml.dump(cfg))


def train():
    config()
    subprocess.run([sys.executable, "-m", "microwakeword.model_train_eval",
                    "--training_config=training_parameters.yaml", "--train", "1",
                    "--restore_checkpoint", "1", "--test_tf_nonstreaming", "0",
                    "--test_tflite_nonstreaming", "0", "--test_tflite_nonstreaming_quantized", "0",
                    "--test_tflite_streaming", "0", "--test_tflite_streaming_quantized", "1",
                    "--use_weights", "best_weights",
                    "mixednet", "--pointwise_filters", "64,64,64,64", "--repeat_in_block", "1, 1, 1, 1",
                    "--mixconv_kernel_sizes", "[5], [7,11], [9,15], [23]",
                    "--residual_connection", "0,0,0,0", "--first_conv_filters", "32",
                    "--first_conv_kernel_size", "5", "--stride", "3"], check=True)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "features"
    if step == "features":
        features("positives", "generated_augmented_features")
        features("adversarial", "adversarial_augmented_features")
    elif step == "train":
        train()
    else:
        raise SystemExit(f"unknown step {step!r}")
