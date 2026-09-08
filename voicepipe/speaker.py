"""Whose voice is this — enrollment, and the gate built on it.

A voiceprint is just a handful of embeddings of the owner speaking, saved to
disk. An utterance is accepted when it scores above `threshold` against the
closest of them.

What this is and isn't: it stops the bridge answering other people in the
room, and it is genuinely useful for that. It is **not** authentication — a
recording of you played back will pass, because it is your voice. Treat it as
a filter on who gets replied to, never as a lock on anything that matters.
"""
import json
import os
import random
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(REPO, "memory", "voiceprint.json")

# Enrollment audio is kept beside the voiceprint, not thrown away. Embeddings
# are one-way, so without the wavs a change of model means re-enrolling from
# scratch, and there is no way to listen back and hear which sample was bad.
# (Learned by losing them: the bridge wrote every utterance to one temp path
# and overwrote it, so a clean 10s clip survived only by luck.)
SAMPLES_DIR = os.path.join(REPO, "memory", "voiceprint_samples")

# Cosine score above which an utterance is accepted.
#
# This default is a starting point, NOT a measurement of any real voice. On a
# controlled synthetic pair (two VITS speaker ids) the clusters were: same
# speaker 0.67-0.77, different speaker 0.34-0.51 — so 0.5 sat close enough to
# the wrong cluster that one impostor clip squeaked through. Real human speech
# separates more cleanly than synthetic voices do, but microphones and rooms
# move the numbers either way.
#
# Tune it against your own recordings before relying on it:
#     python tools/speaker_check.py mine1.wav mine2.wav theirs1.wav
# and put the threshold in the gap between the two clusters. Every utterance
# also logs its score, so the live numbers are there to look at.
DEFAULT_THRESHOLD = 0.5

# Fewer than this and one bad clip (a cough, a truncated word) skews the whole
# voiceprint; the gate warns rather than refusing, since a small print still works.
RECOMMENDED_SAMPLES = 3

# Below this, an embedding is too noisy to decide anything with. Measured by
# truncating a known 10.6s clip of the owner and re-scoring each length
# against his own voiceprint:
#
#     0.75s -> 0.074    1.5s -> 0.617    3.0s -> 0.887
#     1.00s -> 0.122    2.0s -> 0.829    5.0s -> 0.949
#
# i.e. the same person's own voice scores like a total stranger below a
# second, and only becomes solid past two. No threshold can separate anyone
# down there, so a short clip is not judged at all.
#
# It is also NOT let through. Passing unverifiable audio was tried and was
# straightforwardly a hole: a visitor's 1.0-1.7s utterances all sailed past
# while every one of theirs over 2s was correctly rejected (0.139, 0.198,
# 0.277, 0.284). Short clips now get asked to repeat instead — mildly annoying
# for the owner, but the alternative is a gate that anyone can walk through by
# being brief.
MIN_VERIFY_SECONDS = 2.0

# check() verdicts.
ACCEPTED = "accepted"
REJECTED = "rejected"
TOO_SHORT = "too_short"

# What to do with a clip too brief to embed reliably.
#   "ask"   — say a line asking for a longer one. The safe default: a clip
#             that can't be judged isn't a clip that passed.
#   "allow" — let it through unjudged. Convenient for short commands ("yes",
#             "stop") but it is a real hole: anyone can walk past the gate by
#             keeping it under the limit, which is exactly how a visitor's
#             sub-2s utterances were getting answered before the TOO_SHORT
#             verdict existed. Deliberate opt-in only.
SHORT_ASK = "ask"
SHORT_ALLOW = "allow"
SHORT_POLICIES = (SHORT_ASK, SHORT_ALLOW)

# Asking for a longer utterance, in character. Deliberately not one of the
# rejection lines: this is very often the owner, who shouldn't be accused of
# being an impostor for asking a short question.
TOO_SHORT_LINES = [
    "That was too short, say a bit more?",
    "Say that again, but give me a whole sentence.",
    "I didn't get enough of that — say a little more.",
    "Bit short. Try that again for me?",
]


def too_short_line():
    return random.choice(TOO_SHORT_LINES)


# What she says to someone who isn't the owner, escalating with persistence.
# In character rather than a terse error, because being turned away by an
# increasingly furious companion reads as personality instead of a system
# message — and one stranger politely trying twice should get a different
# response from someone jabbing the button ten times.
#
# She is irritable, never abusive: the anger is at the situation and at being
# ignored, not an attack on the person.
OWNER = "Aditya"

REJECTION_TIERS = [
    # 1st refusal — puzzled more than cross
    [
        f"You're not {OWNER}. Who is this?",
        f"Hm. That's not {OWNER}'s voice.",
        f"Wrong person. Where's {OWNER}?",
    ],
    # 2nd-3rd — properly annoyed now
    [
        f"Still not {OWNER}. Give me back to him.",
        f"Nope. Not {OWNER}. Try someone else's things.",
        f"I already said no. I want {OWNER}.",
    ],
    # 4th-5th — genuinely angry
    [
        f"Put {OWNER} on. Right now.",
        f"Stop it. I'm not talking to you, I'm waiting for {OWNER}.",
        f"How many times? Not. {OWNER}. Go away.",
    ],
    # 6th and beyond — done being polite
    [
        f"Enough! Put me down and get {OWNER}.",
        f"I will keep saying no forever. Go find {OWNER}.",
        f"Seriously, stop pressing that. You are not {OWNER} and you never will be.",
    ],
]

# Which tier a given consecutive-rejection count lands in.
_TIER_FOR_STREAK = [0, 0, 1, 1, 2, 2]  # streak 1 -> tier 0, 2-3 -> 1, 4-5 -> 2, 6+ -> last


def rejection_line(streak=1):
    """A refusal, angrier the longer someone has been failing the gate.

    `streak` is the number of consecutive rejections including this one, so
    the first is 1. Anything past the table gets the top tier.
    """
    index = max(1, int(streak))
    tier = _TIER_FOR_STREAK[index] if index < len(_TIER_FOR_STREAK) else len(REJECTION_TIERS) - 1
    return random.choice(REJECTION_TIERS[tier])


# Flat list of every line, for tests and for anyone wanting to see them all.
REJECTION_LINES = [line for tier in REJECTION_TIERS for line in tier]


def _pick(explicit, backend, attribute, fallback):
    """An explicit value, else the backend's own, else the module default."""
    if explicit is not None:
        return explicit
    return getattr(backend, attribute, None) or fallback


def wav_seconds(path):
    """Duration of a wav, or None if it can't be read."""
    import wave
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001 - duration is advisory; a bad read shouldn't gate
        return None


class Voiceprint:
    """The owner's enrolled embeddings, persisted as plain JSON.

    JSON rather than a binary format so it stays inspectable and diffable, and
    so a bad enrollment sample can be deleted by hand.
    """

    def __init__(self, samples=None, path=DEFAULT_PATH, backend=None):
        self.samples = list(samples or [])
        self.path = path
        # Which backend's embeddings these are. Embeddings from two different
        # models are not comparable in any way — the numbers still multiply
        # together and still produce a plausible-looking score, which is
        # exactly why this has to be checked rather than assumed.
        self.backend = backend

    def __len__(self):
        return len(self.samples)

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        """The saved voiceprint, or an empty one if there isn't a usable file."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls(path=path)
        return cls([s["embedding"] for s in data.get("samples", [])],
                   path=path, backend=data.get("backend"))

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "note": "Voice embeddings for the speaker gate. Delete this file to re-enroll.",
            "backend": self.backend,
            "dimensions": len(self.samples[0]) if self.samples else None,
            "enrolled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "samples": [{"embedding": list(map(float, s))} for s in self.samples],
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def add(self, embedding, wav_path=None):
        """Add a sample, keeping a copy of the audio when given one."""
        self.samples.append([float(x) for x in embedding])
        if wav_path:
            self._keep_audio(wav_path, len(self.samples))

    @staticmethod
    def _keep_audio(wav_path, index):
        import shutil
        os.makedirs(SAMPLES_DIR, exist_ok=True)
        try:
            shutil.copy(wav_path, os.path.join(SAMPLES_DIR, f"sample_{index:02d}.wav"))
        except OSError as e:  # noqa: BLE001 - a kept copy is a convenience, not the feature
            print(f"  (couldn't keep a copy of the sample: {e})")

    def score(self, embedding):
        """Best cosine similarity against any enrolled sample, or None if empty.

        Best-of rather than an average: people don't sound identical every
        time, and averaging a tired voice with an energetic one produces a
        centroid that matches neither well.
        """
        if not self.samples:
            return None
        return max(sum(a * b for a, b in zip(sample, embedding)) for sample in self.samples)


class SpeakerGate:
    """Decides whether an utterance is the owner speaking.

    `enabled` is False when there is no voiceprint, so the bridge behaves
    exactly as before until someone actually enrolls — the feature can be
    shipped without changing how an un-enrolled setup works.
    """

    def __init__(self, backend, voiceprint, threshold=None, min_verify_seconds=None,
                 short_policy=SHORT_ASK):
        self.backend = backend
        self.voiceprint = voiceprint
        # Fall back to whatever this backend measured for itself, so a new
        # backend brings its own calibration instead of inheriting numbers
        # that were only ever right for a different model.
        self.threshold = _pick(threshold, backend, "default_threshold", DEFAULT_THRESHOLD)
        self.min_verify_seconds = _pick(
            min_verify_seconds, backend, "min_verify_seconds", MIN_VERIFY_SECONDS)
        if short_policy not in SHORT_POLICIES:
            raise ValueError(f"short_policy must be one of {SHORT_POLICIES}, got {short_policy!r}")
        self.short_policy = short_policy
        self.mismatch = self._backend_mismatch()

    def _backend_mismatch(self):
        """Why this voiceprint can't be used with this backend, or None.

        An untagged voiceprint (written before backends were tagged) is
        allowed through: refusing it would break an existing enrollment for a
        rename, and there is only one backend it can have come from.
        """
        if self.backend is None or not self.voiceprint.samples:
            return None
        name = getattr(self.backend, "name", None)
        if self.voiceprint.backend and name and self.voiceprint.backend != name:
            return (f"voiceprint was enrolled with {self.voiceprint.backend!r} but the "
                    f"gate is running {name!r}; embeddings from different models are not "
                    f"comparable — re-enrol, or switch back")
        dims = getattr(self.backend, "dimensions", None)
        actual = len(self.voiceprint.samples[0])
        if dims and actual != dims:
            return (f"voiceprint holds {actual}-d embeddings but {name} produces "
                    f"{dims}-d — re-enrol")
        return None

    @property
    def enabled(self):
        """Off when there's nothing to compare against, no backend, or the
        voiceprint came from a different model. Off means everyone is
        answered, which is the pre-gate behaviour — a misconfigured gate must
        not silently start judging with meaningless numbers."""
        return bool(self.voiceprint) and self.backend is not None and self.mismatch is None

    def check(self, wav_path):
        """(verdict, score) — one of ACCEPTED / REJECTED / TOO_SHORT.

        `score` is None when no judgement was made. TOO_SHORT means the clip
        is too brief to embed reliably and the caller should ask for a longer
        one; it is emphatically *not* an acceptance, because letting
        unverifiable audio through is a hole anyone can walk through by
        keeping it brief — unless `short_policy` is SHORT_ALLOW, which trades
        exactly that hole for the convenience of short commands.

        Still accepts without checking when the gate is off or the backend
        fails: a broken model should degrade to the previous behaviour rather
        than making the device stop answering everyone.
        """
        if not self.enabled:
            return ACCEPTED, None

        seconds = wav_seconds(wav_path)
        if seconds is not None and seconds < self.min_verify_seconds:
            if self.short_policy == SHORT_ALLOW:
                # Logged every time, not silently: while this is on, the gate
                # has a documented way past it and that should be visible in
                # the log rather than only in the flags it was started with.
                print(f"  (too short to verify: {seconds:.2f}s < "
                      f"{self.min_verify_seconds}s — allowed through, gate bypassed)")
                return ACCEPTED, None
            print(f"  (too short to verify: {seconds:.2f}s < {self.min_verify_seconds}s)")
            return TOO_SHORT, None

        try:
            score = self.voiceprint.score(self.backend.embed(wav_path))
        except Exception as e:  # noqa: BLE001
            print(f"  ! speaker check failed, letting it through: {e}")
            return ACCEPTED, None
        return (ACCEPTED if score >= self.threshold else REJECTED), score
