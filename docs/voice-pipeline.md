# Voice pipeline — speaker verification, output, announcements, memory

Detail behind the current-state summary in `CLAUDE.md`.

## Speaker verification (the gate)

Added 2026-09-05. Only the enrolled owner's voice gets a reply.

- **Model**: WeSpeaker ECAPA-TDNN-512, the official ONNX export from HF
  `Wespeaker/wespeaker-ecapa-tdnn512-LM` (`voxceleb_ECAPA512_LM.onnx`, 24 MB,
  192-d embeddings). Cached at `~/.cache/voicepipe/`, downloaded on first use.
- **Runs in-process in `chat`**, not in its own conda env like the TTS
  backends: it needs only `onnxruntime` and `kaldi-native-fbank` (neither
  pulls torch, both now installed there), and it sits in the path of every
  utterance, where a worker handshake would cost more than the inference.
- **`wespeaker` is not on PyPI** — only `wespeakerruntime`, which drags in
  torchaudio. Hence ONNX + kaldi-native-fbank directly.

### Two things the model is unforgiving about

1. **16 kHz or the embeddings stop discriminating.** Measured: computing fbank
   at a clip's native 24 kHz made same-speaker pairs score *below*
   cross-speaker pairs (0.596 vs 0.708 — worse than chance). After resampling
   to 16 kHz, a controlled pair (two VITS speaker ids, clean audio) gave
   **same speaker 0.665 / 0.767, different speaker 0.511 / 0.342 / 0.371**.
   The bridge already records at 16 kHz so the live path resamples nothing.
2. **Raw int16 sample scale**, not [-1, 1] floats.

### Enrol in the same mic *state* the gate will judge, not just the same room

The bug that broke the first enrollment, worth not repeating. The mic and
speaker share one I2S peripheral: playback runs `M5.Mic.end();
M5.Speaker.begin()`, and the next recording runs `M5.Speaker.end();
M5.Mic.begin()`. `M5.Mic.config()` was applied once in `setup()` and never
re-applied, so a recording made *after* a reply is not in the same state as
one made before any reply.

The first `enroll()` sent only a text `reply:` and no audio, so the firmware
never entered the speaker path and **all five samples were captured in a mic
state that never occurs in conversation**. Measured result: the owner scored
**0.748 on the first utterance (before she had spoken) and 0.17-0.60 on every
one after**, while a different person scored 0.069 — i.e. the model was fine
and the enrollment condition was wrong.

**The fix that mattered was the re-enrollment**, in `Session._say()`: it makes
enrollment speak its confirmations, so the mic is torn down and restarted
between samples exactly as in real use.

`main.cpp` also gained `applyMicConfig()`, called on every `M5.Mic.begin()`
rather than once at boot. **That one turned out to be a no-op** — see
`docs/firmware-notes.md` for the source-level proof. It is flashed and
harmless, but it is not what fixed anything.

The live voiceprint has 33 samples: the original 10 covering both states (1-5
captured before any playback, 6-10 after, self-consistency 0.765-0.895) plus 23
real Stick clips added 2026-09-24 with `tools/voiceprint_add.py`. Because
`Voiceprint.score` is best-of, covering both conditions works without
discarding the earlier samples.

### The threshold is not calibrated for a real voice

`voicepipe/speaker.py`'s generic `DEFAULT_THRESHOLD = 0.5` is a starting point;
the WeSpeaker backend overrides it with its own measured `default_threshold = 0.6`. In the controlled synthetic run
a *different* speaker scored 0.511 and would have been let in.

Measured against real voices on this hardware: **owner 0.765-0.895, a
different person 0.069-0.075** — a wide gap. The bridge was therefore run with
`--speaker-threshold 0.6` (the cluster gateway runs 0.5 since 2026-09-24: real
Stick clips scored 0.54-0.66 against the original enrollment), which leaves room for a tired or more distant voice
while still rejecting a stranger by a large margin. Re-check with
`tools/speaker_check.py` if the mic, room or firmware changes; every utterance
logs its score.

A rejected utterance gets one of `speaker.REJECTION_LINES` spoken at random
("You're not Aditya! Give me back to him.") rather than a silent caption, so
whoever tripped it hears why.

### Short utterances: `--short-utterances {ask,allow}`

Added 2026-09-07. A clip under `min_verify_seconds` can't be embedded
reliably — measured by truncating one known-good recording, the *same*
speaker scored **0.074 at 0.75s, 0.617 at 1.5s, 0.829 at 2.0s**. The
information isn't there yet, and no threshold separates anyone below ~2s.

- **`ask`** (default) returns `TOO_SHORT` and speaks a `TOO_SHORT_LINES` line
  asking for a longer one. It does not count toward the anger streak, because
  it is usually the owner being brief rather than a stranger.
- **`allow`** waves anything shorter straight through **unverified**. This is
  a real hole — it is precisely how a visitor's sub-2s utterances were being
  answered before the `TOO_SHORT` verdict existed — so it prints a warning at
  startup and logs `gate bypassed` on every use. Run with it deliberately.

Running with `allow` as of 2026-09-07, at the owner's request.

The bridge was also run with `--encourage --encourage-interval 28 32` (see
below; the cluster gateway currently runs without `--encourage`) — a
half-hour cadence, jittered rather than exactly 30 so it doesn't land on the
clock.

**Rejected: a second SV model for short clips.** ECAPA-TDNN pools statistics
over the utterance, so short-duration degradation is inherent to the model
family, not specific to WeSpeaker — a second model sits on the same curve.
It would also need its own separate enrollment (embeddings across models
aren't comparable; see the mismatch guard above) and a lower threshold, which
rebuilds the same hole with two models resident. The cheaper fix, if the
convenience is wanted back without the hole, is a **recently-verified
window**: trust short clips for ~60s after an accepted utterance, reset on any
rejection. Not built yet.

**This is a filter, not authentication.** A recording of the owner passes,
because it is the owner's voice. It stops other people in the room being
answered; it must not gate anything that matters.

### Usage

    python bridge_server.py --enroll        # hold the button, say a sentence, x5
                                            # (she speaks between samples — that
                                            #  playback is part of the point)
    python bridge_server.py                 # gate is on automatically once enrolled
    python tools/speaker_check.py a.wav b.wav   # pick a threshold
    python bridge_server.py --no-speaker-check  # temporarily answer anyone

Enrollment goes through the Stick deliberately: the print must come from the
same mic, codec and room the gate will judge against. Samples live in
`memory/voiceprint.json`; delete it to start over. With no voiceprint the gate
is off and the bridge behaves exactly as it did before.

## Output volume — the amplifier was maxed, the signal wasn't

Measured 2026-09-07. `main.cpp` has had `M5.Speaker.setVolume(255)` (full
scale) since the first commit, so the *device* was already as loud as it goes.
The signal reaching it was not: a real VITS reply measured
**-3.5 dB peak / -18.1 dB mean**, i.e. the amplifier was at 100% driving audio
at roughly two thirds amplitude.

`resample_to_pcm16()` now runs ffmpeg's one-pass `speechnorm` filter
(`NORMALIZE_FILTER = "speechnorm=e=6.25:r=0.00001:l=1"`), on by default,
disabled with **`--no-normalize`**. Measured on a live reply, before and
after:

| | peak | mean |
| --- | ---: | ---: |
| raw | -4.5 dB | -15.0 dB |
| normalized | **-0.4 dB** | **-10.6 dB** |

Chosen over flat gain (`volume=+4dB`), which would clip the louder chunks, and
over `loudnorm`, which wants two passes. `speechnorm` also lifts quiet
syllables rather than scaling everything. It runs at ~153x realtime, so it
costs nothing next to the second already spent in TTS. Replies and
announcements both go through it, so an encouragement isn't quieter than an
answer.

Unchanged caveat: 255 is above M5Stack's own ≤191 guidance for battery
operation. Louder audio draws more, so if brownout reboots ever appear during
playback on battery, this is the first thing to turn down.

## Proactive announcements

`Session.announce(text)` speaks without anyone pressing the button, and
`--announce-socket` (default `/tmp/rina-announce.sock`) exposes it: one line
in, one announcement out. `tools/say.py` is the client.

**Confirmed working 2026-09-05 against the real Stick, with no firmware
change.** The wire protocol turns out not to care who started a turn —
`reply:` / audio / `end` just means "display and play", and `webSocketEvent`
never checks whether a question is outstanding. This is the foundation for
reminders, timers and alerts.

    python tools/say.py "the build finished"

### Announcements can no longer interleave with a turn

Added 2026-09-07, with the encouragement loop below. `reply:` / audio / `end`
is a *frame sequence*, not a message, so an announcement that started while a
turn was mid-flight would interleave its PCM with the reply's and the Stick
would play both as noise. `Session.speaking` is an `asyncio.Lock` held by
`handle_utterance()` and by `announce()`, so an unprompted line waits for the
turn to finish rather than corrupting it. This was latent for `tools/say.py`
too, not just for the new loop.

## Encouragement (`--encourage`)

Added 2026-09-07. She says something encouraging unprompted every so often:

    python bridge_server.py --encourage                       # every 15-20 min
    python bridge_server.py --encourage --encourage-interval 30 45

- Lines live in `voicepipe/encouragement.py` as a **static list**, not
  generated. Generating one would cost a full LLM turn every quarter hour,
  keep a model or agent warm for nothing, and can wander off-persona or fail
  while nobody is watching. The list is instant, needs no GPU, works offline.
- `line()` avoids anything used in the last `NO_REPEAT_WINDOW` (8) draws —
  hearing the same encouragement twice in half an hour reads as mechanical.
- The name comes from `speaker.OWNER` via a `{owner}` placeholder, so it is
  spelled in one place. Roughly a third of the lines use "-senpai"; every
  line doing so stops landing as affection.
- The interval is drawn fresh from `[MIN, MAX]` each time rather than fixed,
  so it doesn't read as a cron job. `bridge_server.encourage_loop()` builds
  on `announce()`, so **no firmware change** is needed.
- No Stick connected: the line is dropped and logged, never queued —
  encouragement that arrives an hour late is worse than none. Any exception is
  caught so one failure can't kill the task.

Verified 2026-09-07 through the real entrypoint (`--encourage-interval 0.05
0.08` on port 8766) — flags parse, the task starts, ticks fire, and the
no-Stick path logs rather than throwing.

## Memory

`memory/about-me.md` holds hand-written facts about Aditya. `voicepipe/
personas.py` reads it fresh at every start (no rebuild needed) and appends it
to the system prompt under a header telling the model to use it silently.
`--profile PATH` points elsewhere, `--no-profile` disables it.

Everything inside HTML comments is stripped, which is how the shipped template
costs nothing until it is filled in — the unfilled file contributes 27 chars.
This matters: the profile is spent on *every* turn, and the 1650's model has a
4096-token context (the 4060's has 40960), so `load_profile()` warns above 4000
chars.

This is the static half of memory. Since 2026-09-25 she can also read and
write one notes file, `~/rina/notes.md` on arch-ssd, which the owner edits too
(`read_notes`/`add_note`/`write_notes`, see `docs/companion-control-mcp.md`).
Searchable memory doesn't exist yet. Keep all of that out of the profile: it is
the always-resident slice, not a store.

## False wake-ups: letting her stay silent (2026-09-25)

On the first evening of wake-word use, Rina answered six fragments in 16
minutes that nobody had said to her ("Prani", "All.", "Then g-f is one of the
other g-f is one"): the wake word firing on nearby talk, then hands-free
recording whatever followed. Two changes:

- **Stick events.** The Stick now reports what it decides on its own
  (`FRAME_EVENT`, app 1.6, relayed as `event:…`): `wake fired <mean>`,
  `turn wake|button|btnb` just before a turn's start, `wake near peak <p>
  mean <m>` for a ~2 s window that got to half the cutoff without firing, and
  `vad nothing heard after wake|btnb`. The gateway prints them (`[stick] …`),
  writes them to `events-YYYY-MM-DD.jsonl` beside the conversation log, and
  puts `trigger` (and `wake_score`) on the turn's line. "Why didn't that go
  through?" and "how often does it false-trigger?" are now answerable from
  the server.
- **Silence.** On a wake-word turn only (a button press is meant), the user
  message sent to the model carries a note: if this is a fragment, garbled,
  or clearly someone talking to somebody else, reply with exactly
  `[silent]`. The note is on a copy, not in the history, and not a
  mid-conversation system message (some chat templates reject those). A
  `[silent]` reply is never spoken, on any turn: the gateway sends only
  `end` (the Stick goes back to idle), drops the fragment from the history,
  and logs the turn with `silent: true`.

## Voice isolation, step one: gain and distance (2026-09-25)

The StickS3 has **one** microphone (ES8311, MIC1P/N), so nothing on the
device can separate voices by direction (that takes two or more mics), and
M5Unified's `noise_filter_level` is only a one-pole low-pass: less hiss, more
muffled consonants, no isolation. What the recordings did show, measured over
24 logged turns (`voicepipe/audio_levels.py`: loud frames = 90th percentile of
30 ms RMS, floor = 10th):

- **The owner's speech clipped.** Most turns peaked at 0 dBFS, up to 9.4% of
  samples clipped ("what's the time right now" became "raise the shine right
  now", 3.5% clipped). The gain chain is the ES8311's analog PGA at minimum,
  its digital ADC volume at maximum (0xFF, about +32 dB), then M5Unified's
  software `magnification` of 16. The firmware now sets 8 (-6 dB).
- **Distance separates the false triggers.** Speech meant for Rina: loud
  frames -2 to -12 dBFS, 20-36 dB above the room. Fragments the wake word
  caught from nearby talk: -13 to -25 dBFS, 8-16 dB above it.

So every turn's levels now go into the conversation log (`levels`) and the
gateway log (`levels speech=… floor=… snr=… clipped=…`). Next, once there
are turns at the new gain: a threshold that ignores speech too quiet or too
close to the room's level to have been aimed at the Stick. Server-side noise
suppression (DeepFilterNet/RNNoise) and keeping only the owner's segments by
voiceprint are the heavier steps after that.

## Conversation log (2026-09-25)

`--conversation-log DIR` keeps every turn the gateway handled, for analysis:
one JSON line per turn in `DIR/YYYY-MM-DD.jsonl` and that turn's recording in
`DIR/audio/<id>.wav`. A line has `id`, `time`, `audio_seconds`, `speaker`
(verdict and score), `heard` (the STT transcript), `reply`, `tools` (the tool
call and result messages), `stages` (seconds for stt, agent, tts+wire),
`seconds` for the whole turn, and `error` when something failed. Refused turns
are logged too, with their verdict and no transcript.

Only what the Stick sent is logged: the wake word and the end-of-speech
detector run on the Stick, and it sends nothing until a turn starts (wake word
or a button hold). Announcements and encouragement lines are not turns and
aren't logged.

The cluster writes to `/var/lib/aicompanion/conversations` on arch-ssd
(`gateway.yaml`). Recordings past `--conversation-log-max-gb` (2) are deleted
oldest first; the transcript lines stay. This is separate from
`--keep-utterances` (the newest 50 clips, for the voiceprint). While the speaker
check is off, it records anyone who speaks to the Stick, not only the owner.


## Adding samples to the voiceprint without re-enrolling (2026-09-24)

The 10 enrolled samples (2026-09-05) agree with each other (0.77-0.90 leave-one-out)
but real Stick utterances score only 0.54-0.66 against them, and scoring is
best-of, so what helps is samples that sound like how the owner actually talks to
it now. `bridge_server.py --enroll` doesn't exist in the cluster gateway, so:

1. The gateway runs with `--keep-utterances /utterances` (a node hostPath,
   `/var/lib/aicompanion/utterances`, newest 50, any verdict), naming each clip
   `<time>_<verdict>_<score>.wav`.
2. `tools/voiceprint_add.py list` scores every kept clip against the current
   voiceprint; `add <clip>...` appends the chosen ones (backing up
   `voiceprint.json`, keeping the audio in `memory/voiceprint_samples/`);
   `apply --yes` updates the `aicompanion-personal-data` Secret and restarts the
   gateway.
3. `add` refuses clips under 2s or scoring under 0.4 against the current
   voiceprint unless `--force`: best-of scoring means one sample that isn't the
   owner lets that voice in.

The clips are biometric data: they stay on the node, are pruned to 50, and are
not in git (`memory/voiceprint_samples/` is gitignored).


## Why the 2-second minimum stays (measured 2026-09-24)

With 33 samples enrolled, each of the owner's 23 real Stick clips was cut to its
first N seconds (the way a short utterance looks, leading silence included) and
scored leave-one-out against the other 32 samples:

| length | owner min | owner mean | owner clips under 0.5 |
|---|---|---|---|
| 1.0s | 0.252 | 0.460 | 14 / 23 |
| 1.2s | 0.318 | 0.496 | 11 / 23 |
| 1.5s | 0.382 | 0.561 | 6 / 23 |
| 2.0s | 0.541 | 0.644 | 0 / 23 |

Lowering `--speaker-min-seconds` to 1.5s would reject the owner about one time in
four, and lowering the threshold to compensate would approach strangers' 0.28.
So sub-2s clips stay unjudged, and `--short-utterances` decides what happens to
them: the cluster gateway runs `allow` (answered unchecked, owner's choice), with
`ask` as the code default. Revisit before any tool that acts (Spotify, notes) is
reachable by voice.
