# On-device wake words — training pipeline

The on-device wake word (`firmware/m5stick_bridge/src/wake_word.h`) runs a
microWakeWord streaming model trained with these scripts. Everything here runs
in a workspace outside the repo (`~/mww-train`); the repo keeps the scripts and
the resulting model (`firmware/m5stick_bridge/models/rina_chan.tflite`).

## Steps

```bash
mkdir -p ~/mww-train && cp tools/wakeword/* ~/mww-train/ && cd ~/mww-train
uv venv --python 3.10 .venv && source .venv/bin/activate
git clone https://github.com/kahrendt/microWakeWord          # tested at 4665173
git -C microWakeWord apply ../microwakeword-local.patch        # see below
git clone https://github.com/rhasspy/piper-sample-generator
curl -sSL -o piper-sample-generator/models/en_US-libritts_r-medium.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
uv pip install -e ./microWakeWord 'tensorflow[and-cuda]' tensorboard "setuptools<81" \
  "audiomentations>=0.34" torch torchaudio piper-phonemize-cross==1.2.1 -e ./piper-sample-generator
python prepare_data.py          # ~26 GB: noise, room echoes, negative features
./gen_samples.sh                # 10k "Rina-chan" clips + 3.6k look-alikes, CPU, ~15 min
python train_rina.py features   # augmented spectrograms, CPU
./watchdog.sh 5000 & ./commit_guard.sh 38 &
MWW_MAX_EVAL_PER_SET=4000 ./run_train.sh   # 20k steps on the GPU, ~45 min
```

Then copy `trained_models/rina_chan/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite`
to `firmware/m5stick_bridge/models/rina_chan.tflite` and run
`python scripts/tflite_to_header.py models/rina_chan.tflite models/rina_chan.json`
in `firmware/m5stick_bridge/`.

## Wake words

The firmware compiles in one model, `src/wake_word_model.h`, generated from a
`models/*.tflite` + manifest pair. Two are trained:

- **"Assistant"**: `models/assistant.tflite`, cutoff 0.90.
  `WAKE=assistant` selects `positives_assistant/`, `adversarial_assistant/`
  and `trained_models/assistant/` in the scripts; samples from
  `gen_samples_assistant.sh`.
- **"Rina-chan"** (current, flashed 2026-09-25): `models/rina_chan.tflite`, cutoff 0.99 (`WAKE` unset).

Switch with, in `firmware/m5stick_bridge/`:
`python scripts/tflite_to_header.py models/rina_chan.tflite models/rina_chan.json`
(or the assistant pair), then build and flash.

"Assistant" is an everyday word, so it also fires when said mid-sentence;
look-alikes (assist, assistance, insistent, resistant, a sister, instant, …)
are trained as negatives, but containing phrases ("assistant manager",
"assistants") were deliberately left out: labelling them negative would
contradict the positives.

## Memory: read this before running

On this laptop (WSL capped at 10 GB, Windows commit limit ~41 GB) an unguarded
run OOM-killed WSL and every session in it, twice (2026-09-24). Causes and fixes:

- microWakeWord loaded **every** validation spectrogram into one Python list,
  then copied it into a NumPy array: 10.8 GB at the first evaluation.
  `microwakeword-local.patch` caps each set per evaluation pass
  (`MWW_MAX_EVAL_PER_SET`, default 10000; 4000 used).
- TensorFlow reserves all VRAM by default, and under WSL GPU memory is charged
  to Windows' commit. `run_train.sh` sets `TF_FORCE_GPU_ALLOW_GROWTH=true`
  (the model needs ~1.5 GB).
- `watchdog.sh` kills training above an anonymous-RSS limit (file-mapped pages
  of the 30+ GB of mmapped features are reclaimable and don't count);
  `commit_guard.sh` kills it when Windows commit passes a limit. Both leave the
  checkpoints intact: rerunning resumes from them.

The patch also swaps microWakeWord's `datasets.Audio` clip loading for
soundfile + scipy resampling, because `datasets` now decodes with torchcodec,
which segfaults when loaded alongside TensorFlow.

## Result (2026-09-25)

20k steps (plus 8k before a pause). Quantized streaming model, 62 KB, test set:
cutoff 0.99 → 9.8% false rejections, 2.0 false accepts per hour (a ~1 h
ambient test set, so a rough figure). The firmware uses cutoff 252/255 with a
5-window mean. First real use through the Stick: 12 of ~12 "Rina-chan"s
detected, no false triggers during a minute of normal talk, 2.8 ms per
inference every 30 ms. The data is non-commercial personal use only (mixed
licenses of the negative and background sets).

"Assistant" (2026-09-25), same recipe, 20k steps: cutoff 0.91 → 13.3% false
rejections, 0 false accepts/hour; 0.90 → 12.6%, 0.19/hour; 0.86 → 10.7%,
0.56/hour. A short first test on the Stick: 2 of 2 detected (249 and 251/255).
