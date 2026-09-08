"""The speaker gate: the voiceprint store, the accept/reject decision, and the
bridge refusing a stranger before it spends anything on the turn.

The embedding backend is faked here — the real model has its own test below,
marked so it can be skipped when the ONNX file isn't downloaded.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bridge_server
from conftest import FakeWebSocket
from voicepipe.speaker import (
    ACCEPTED, MIN_VERIFY_SECONDS, OWNER, REJECTED, REJECTION_LINES, REJECTION_TIERS,
    DEFAULT_THRESHOLD, SHORT_ALLOW, SHORT_ASK, TOO_SHORT, TOO_SHORT_LINES,
    SpeakerGate, Voiceprint,
    rejection_line, too_short_line, wav_seconds,
)


def unit(*values):
    """A unit-length vector, so dot products read as cosine scores."""
    total = sum(v * v for v in values) ** 0.5
    return [v / total for v in values]


def fake_backend(embedding, name="wespeaker-ecapa-tdnn512", dimensions=2,
                 threshold=0.6, min_seconds=2.0):
    """A stand-in that declares the same attributes a real backend does.

    A bare Mock() auto-creates those as Mock objects, which then compare
    nonsensically against durations and scores — so they're set explicitly.
    """
    backend = Mock()
    backend.embed.return_value = embedding
    backend.name = name
    backend.dimensions = dimensions
    backend.default_threshold = threshold
    backend.min_verify_seconds = min_seconds
    return backend


ME = unit(1.0, 0.0)
ALSO_ME = unit(0.95, 0.31)     # ~0.95 against ME
SOMEONE_ELSE = unit(0.0, 1.0)  # 0.0 against ME


class TestVoiceprint:
    def test_a_missing_file_loads_empty(self, tmp_path):
        assert len(Voiceprint.load(tmp_path / "nope.json")) == 0

    def test_a_corrupt_file_loads_empty_rather_than_crashing(self, tmp_path):
        p = tmp_path / "vp.json"
        p.write_text("{not json")
        assert len(Voiceprint.load(p)) == 0

    def test_round_trips_through_disk(self, tmp_path):
        p = tmp_path / "vp.json"
        vp = Voiceprint(path=p)
        vp.add(ME)
        vp.add(ALSO_ME)
        vp.save()

        assert len(Voiceprint.load(p)) == 2

    def test_saved_file_is_readable_json_with_a_note(self, tmp_path):
        """It lives in memory/ next to hand-edited files, so a human opening it
        should be able to tell what it is and how to remove it."""
        p = tmp_path / "vp.json"
        vp = Voiceprint(path=p)
        vp.add(ME)
        vp.save()

        data = json.loads(p.read_text())
        assert "re-enroll" in data["note"]
        assert len(data["samples"]) == 1

    def test_score_is_the_best_match_not_the_average(self):
        """People don't sound identical every time; averaging a tired voice
        with an energetic one matches neither."""
        vp = Voiceprint([SOMEONE_ELSE, ME])
        assert vp.score(ME) == pytest.approx(1.0)

    def test_an_empty_voiceprint_scores_nothing(self):
        assert Voiceprint().score(ME) is None


class TestSpeakerGate:
    def _gate(self, samples, threshold=DEFAULT_THRESHOLD, embedding=ME):
        return SpeakerGate(fake_backend(embedding), Voiceprint(samples), threshold)

    def test_disabled_without_a_voiceprint(self):
        gate = self._gate([])
        assert not gate.enabled
        assert gate.check("x.wav") == (ACCEPTED, None)

    def test_disabled_without_a_backend(self):
        assert not SpeakerGate(None, Voiceprint([ME])).enabled

    def test_the_owner_is_accepted(self):
        verdict, score = self._gate([ME], embedding=ALSO_ME).check("x.wav")
        assert verdict == ACCEPTED and score > 0.9

    def test_a_stranger_is_rejected(self):
        verdict, score = self._gate([ME], embedding=SOMEONE_ELSE).check("x.wav")
        assert verdict == REJECTED and score < 0.1

    def test_the_threshold_is_what_decides(self):
        assert self._gate([ME], threshold=0.99, embedding=ALSO_ME).check("x")[0] == REJECTED
        assert self._gate([ME], threshold=0.90, embedding=ALSO_ME).check("x")[0] == ACCEPTED

    def test_a_score_exactly_on_the_threshold_is_accepted(self):
        gate = self._gate([ME], threshold=1.0, embedding=ME)
        assert gate.check("x.wav")[0] == ACCEPTED

    def test_a_broken_backend_lets_the_utterance_through(self):
        """A failed model should degrade to the old behaviour, not silently
        make the device stop answering anyone."""
        backend = fake_backend(ME)
        backend.embed.side_effect = RuntimeError("model missing")
        gate = SpeakerGate(backend, Voiceprint([ME]))

        assert gate.check("x.wav") == (ACCEPTED, None)


class TestBridgeGate:
    # Three seconds: long enough to clear MIN_VERIFY_SECONDS, so these
    # exercise the judged path rather than the too-short-to-judge one.
    LOUD = b"\x10\x00" * (bridge_server.SAMPLE_RATE * 3)

    def _session(self, gate):
        args = SimpleNamespace(model="rina", system="be nice", think=False, enroll=False)
        return bridge_server.Session(llm=Mock(spec=["ask"]), stt=Mock(), stt_lang="en",
                                     voice=None, args=args, gate=gate)

    def _backend(self, embedding):
        return fake_backend(embedding)

    async def test_a_stranger_is_refused_without_running_stt_or_the_llm(self):
        """The gate is before STT on purpose: a stranger should cost one
        embedding, not a whole transcribe-and-generate turn."""
        backend = fake_backend(SOMEONE_ELSE)
        session = self._session(SpeakerGate(backend, Voiceprint([ME])))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        session.stt.transcribe.assert_not_called()
        session.llm.ask.assert_not_called()
        assert ws.sent[0].startswith("reply:")
        assert any(line in ws.sent[0] for line in REJECTION_LINES)
        assert ws.sent[-1] == "end"

    async def test_the_owner_goes_through_normally(self):
        backend = fake_backend(ALSO_ME)
        session = self._session(SpeakerGate(backend, Voiceprint([ME])))
        session.stt.transcribe = Mock(return_value="hello")
        session.llm.ask = Mock(return_value="hi")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent == ["heard:hello", "reply:hi", "end"]

    async def test_no_gate_behaves_exactly_as_before(self):
        session = self._session(None)
        session.stt.transcribe = Mock(return_value="hello")
        session.llm.ask = Mock(return_value="hi")
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent == ["heard:hello", "reply:hi", "end"]

    async def test_a_rejected_turn_still_ends(self):
        backend = fake_backend(SOMEONE_ELSE)
        session = self._session(SpeakerGate(backend, Voiceprint([ME])))
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD)

        assert ws.sent[-1] == "end"


class TestRejectionLines:
    """Turning someone away is in character rather than a system message, and
    gets angrier the longer they keep trying."""

    def test_there_are_several_so_it_does_not_get_stale(self):
        assert len(REJECTION_LINES) >= 7

    def test_every_line_names_the_owner(self):
        assert all(OWNER in line for line in REJECTION_LINES)

    def test_lines_are_distinct(self):
        assert len(set(REJECTION_LINES)) == len(REJECTION_LINES)

    def test_rejection_line_returns_one_of_them(self):
        assert rejection_line() in REJECTION_LINES

    def test_it_varies_across_calls(self):
        """A fixed line would be indistinguishable from a stuck device."""
        assert len({rejection_line(1) for _ in range(60)}) > 1

    def test_no_line_starts_with_a_bracket(self):
        """The firmware reads a leading '(' as an error reply and flashes the
        error face; these are meant to be spoken in character."""
        assert not any(line.startswith("(") for line in REJECTION_LINES)

    def test_the_first_refusal_is_the_mildest_tier(self):
        assert all(rejection_line(1) in REJECTION_TIERS[0] for _ in range(30))

    def test_persistence_escalates_through_the_tiers(self):
        seen = [next(i for i, tier in enumerate(REJECTION_TIERS) if rejection_line(s) in tier)
                for s in (1, 3, 5, 8)]
        assert seen == sorted(seen)      # never gets calmer
        assert seen[0] < seen[-1]         # and does actually escalate

    def test_a_long_streak_stays_at_the_top_tier(self):
        assert all(rejection_line(50) in REJECTION_TIERS[-1] for _ in range(20))

    def test_streak_zero_or_negative_is_treated_as_the_first(self):
        """Defensive: a miscounted streak shouldn't crash or skip tiers."""
        assert rejection_line(0) in REJECTION_TIERS[0]
        assert rejection_line(-5) in REJECTION_TIERS[0]


class TestRejectionStreak:
    LOUD = b"\x10\x00" * (bridge_server.SAMPLE_RATE * 3)

    def _session(self, embedding):
        from types import SimpleNamespace
        args = SimpleNamespace(model="rina", system="be nice", think=False, enroll=False)
        gate = SpeakerGate(fake_backend(embedding), Voiceprint([ME]), threshold=0.6)
        return bridge_server.Session(llm=Mock(spec=["ask"]), stt=Mock(), stt_lang="en",
                                     voice=None, args=args, gate=gate)

    async def test_the_streak_climbs_with_each_rejection(self):
        session = self._session(SOMEONE_ELSE)
        for expected in (1, 2, 3):
            await session.handle_utterance(FakeWebSocket(), self.LOUD)
            assert session.rejection_streak == expected

    async def test_an_accepted_utterance_resets_it(self):
        """Otherwise one stray rejection leaves her shouting at the owner."""
        session = self._session(SOMEONE_ELSE)
        await session.handle_utterance(FakeWebSocket(), self.LOUD)
        assert session.rejection_streak == 1

        session.gate.backend.embed.return_value = ME
        session.stt.transcribe = Mock(return_value="hi")
        session.llm.ask = Mock(return_value="hello")
        await session.handle_utterance(FakeWebSocket(), self.LOUD)

        assert session.rejection_streak == 0


def write_wav(path, seconds, rate=16000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return str(path)


class TestShortUtterances:
    """An embedding from under ~2s of speech is noise: the owner's own voice
    scored 0.074 at 0.75s and 0.829 at 2.0s against his own print. Deciding on
    that number rejected him constantly, so short clips aren't judged at all."""

    def _gate(self, embedding=SOMEONE_ELSE):
        return SpeakerGate(fake_backend(embedding), Voiceprint([ME]), threshold=0.6)

    def test_a_short_clip_is_not_judged_and_not_let_through(self, tmp_path):
        """Letting unverifiable audio through was a hole anyone could walk
        through by being brief — a visitor's sub-2s utterances all passed
        while every one of theirs over 2s was correctly rejected."""
        gate = self._gate()
        verdict, score = gate.check(write_wav(tmp_path / "s.wav", 1.4))

        assert verdict == TOO_SHORT
        assert score is None                    # no judgement was made
        gate.backend.embed.assert_not_called()  # and nothing was spent making one

    def test_a_long_enough_clip_is_judged_normally(self, tmp_path):
        gate = self._gate()
        verdict, score = gate.check(write_wav(tmp_path / "l.wav", 4.0))

        assert verdict == REJECTED and score is not None
        gate.backend.embed.assert_called_once()

    def test_the_boundary_is_inclusive(self, tmp_path):
        """Exactly MIN_VERIFY_SECONDS is long enough to judge."""
        gate = self._gate()
        gate.check(write_wav(tmp_path / "b.wav", MIN_VERIFY_SECONDS))
        gate.backend.embed.assert_called_once()

    def test_even_the_owner_is_asked_to_repeat_when_short(self, tmp_path):
        """No threshold separates anyone below 2s, so it can't be waved
        through for the owner either — it simply isn't knowable."""
        gate = self._gate(embedding=ME)
        assert gate.check(write_wav(tmp_path / "s.wav", 1.0))[0] == TOO_SHORT


class TestWavSeconds:
    def test_reads_a_duration(self, tmp_path):
        assert wav_seconds(write_wav(tmp_path / "a.wav", 3.0)) == pytest.approx(3.0)

    def test_an_unreadable_file_is_not_fatal(self, tmp_path):
        bad = tmp_path / "bad.wav"
        bad.write_text("not a wav")
        assert wav_seconds(str(bad)) is None

    def test_a_missing_file_is_not_fatal(self):
        assert wav_seconds("/nonexistent.wav") is None


class TestTooShortHandling:
    LOUD_SHORT = b"\x10\x00" * int(bridge_server.SAMPLE_RATE * 1.2)
    LOUD_LONG = b"\x10\x00" * (bridge_server.SAMPLE_RATE * 3)

    def _session(self, embedding):
        from types import SimpleNamespace
        args = SimpleNamespace(model="rina", system="be nice", think=False, enroll=False)
        gate = SpeakerGate(fake_backend(embedding), Voiceprint([ME]), threshold=0.6)
        return bridge_server.Session(llm=Mock(spec=["ask"]), stt=Mock(), stt_lang="en",
                                     voice=None, args=args, gate=gate)

    async def test_a_short_utterance_never_reaches_stt_or_the_llm(self):
        session = self._session(SOMEONE_ELSE)
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD_SHORT)

        session.stt.transcribe.assert_not_called()
        session.llm.ask.assert_not_called()

    async def test_it_asks_for_more_rather_than_accusing(self):
        session = self._session(SOMEONE_ELSE)
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD_SHORT)

        spoken = ws.sent[0]
        assert any(line in spoken for line in TOO_SHORT_LINES)
        assert not any(line in spoken for line in REJECTION_LINES)

    async def test_short_utterances_do_not_stoke_the_anger_streak(self):
        """Usually it's the owner asking something brief; they shouldn't be
        escalated at for it."""
        session = self._session(ME)
        for _ in range(4):
            await session.handle_utterance(FakeWebSocket(), self.LOUD_SHORT)

        assert session.rejection_streak == 0

    async def test_the_turn_still_ends(self):
        session = self._session(SOMEONE_ELSE)
        ws = FakeWebSocket()

        await session.handle_utterance(ws, self.LOUD_SHORT)

        assert ws.sent[-1] == "end"


class TestTooShortLines:
    def test_there_are_several(self):
        assert len(TOO_SHORT_LINES) >= 3

    def test_none_of_them_accuse_the_speaker(self):
        assert not any(OWNER in line for line in TOO_SHORT_LINES)

    def test_too_short_line_returns_one(self):
        assert too_short_line() in TOO_SHORT_LINES


class TestBackendIsPluggable:
    """Adding a speaker backend should be one file, and swapping to one must
    not silently reuse a voiceprint the old model produced."""

    def test_a_backends_own_calibration_is_used_by_default(self):
        gate = SpeakerGate(fake_backend(ME, threshold=0.42, min_seconds=3.5), Voiceprint([ME]))
        assert gate.threshold == 0.42
        assert gate.min_verify_seconds == 3.5

    def test_an_explicit_flag_still_wins(self):
        gate = SpeakerGate(fake_backend(ME, threshold=0.42), Voiceprint([ME]), threshold=0.9)
        assert gate.threshold == 0.9

    def test_a_voiceprint_from_another_backend_turns_the_gate_off(self):
        """Embeddings from two models still multiply into a plausible-looking
        score, so this has to be detected rather than assumed."""
        vp = Voiceprint([ME], backend="some-other-model")
        gate = SpeakerGate(fake_backend(ME, name="wespeaker-ecapa-tdnn512"), vp)

        assert gate.mismatch is not None
        assert not gate.enabled
        assert gate.check("x.wav") == (ACCEPTED, None)   # off means answer everyone

    def test_a_dimension_change_is_caught_too(self):
        vp = Voiceprint([ME], backend="m")
        gate = SpeakerGate(fake_backend(ME, name="m", dimensions=512), vp)
        assert "512" in gate.mismatch

    def test_a_matching_backend_is_fine(self):
        vp = Voiceprint([ME], backend="m")
        gate = SpeakerGate(fake_backend(ME, name="m", dimensions=2), vp)
        assert gate.mismatch is None and gate.enabled

    def test_an_untagged_voiceprint_is_still_accepted(self):
        """Enrollments predate the tagging; refusing them would break a
        working setup over a bookkeeping change."""
        gate = SpeakerGate(fake_backend(ME), Voiceprint([ME], backend=None))
        assert gate.mismatch is None and gate.enabled

    def test_the_backend_is_recorded_when_saved(self, tmp_path):
        vp = Voiceprint([ME], path=tmp_path / "vp.json", backend="wespeaker-ecapa-tdnn512")
        vp.save()

        reloaded = Voiceprint.load(tmp_path / "vp.json")
        assert reloaded.backend == "wespeaker-ecapa-tdnn512"

    def test_the_real_backend_satisfies_the_protocol(self):
        """isinstance, not issubclass: a runtime_checkable Protocol carrying
        non-method members (the calibration attributes) rejects issubclass by
        design. Constructing is cheap — the model loads lazily."""
        from voicepipe.registry import SV, SpeakerBackend
        for name in SV.names():
            backend = SV.lookup(name)()
            assert isinstance(backend, SpeakerBackend), name
            for attr in ("name", "dimensions", "default_threshold", "min_verify_seconds"):
                assert getattr(backend, attr, None) is not None, f"{name}.{attr}"


def short_gate(tmp_path, short_policy=SHORT_ASK):
    """A gate that will reject anything it actually scores, so an ACCEPTED
    verdict can only have come from the short-utterance path."""
    voiceprint = Voiceprint(path=str(tmp_path / "vp.json"), samples=[ME])
    return SpeakerGate(fake_backend(SOMEONE_ELSE), voiceprint,
                       short_policy=short_policy)


class TestShortUtterancePolicy:
    """A clip under min_verify_seconds can't be embedded reliably — measured
    on one real voice, the same speaker scored 0.074 at 0.75s and 0.829 at
    2.0s. What happens to it is a deliberate choice, not a default."""

    def test_ask_is_the_default(self, tmp_path):
        gate = short_gate(tmp_path)
        assert gate.short_policy == SHORT_ASK
        assert gate.check(write_wav(tmp_path / "a.wav", 0.5))[0] == TOO_SHORT

    def test_allow_lets_it_through_unjudged(self, tmp_path):
        gate = short_gate(tmp_path, short_policy=SHORT_ALLOW)
        verdict, score = gate.check(write_wav(tmp_path / "a.wav", 0.5))
        assert verdict == ACCEPTED
        assert score is None  # nothing was scored; it was waved past

    def test_allow_says_so_in_the_log(self, tmp_path, capsys):
        gate = short_gate(tmp_path, short_policy=SHORT_ALLOW)
        gate.check(write_wav(tmp_path / "a.wav", 0.5))
        assert "gate bypassed" in capsys.readouterr().out

    def test_allow_does_not_weaken_long_utterances(self, tmp_path):
        """The hole is exactly as wide as min_verify_seconds and no wider."""
        gate = short_gate(tmp_path, short_policy=SHORT_ALLOW)
        verdict, score = gate.check(write_wav(tmp_path / "b.wav", 3.0))
        assert verdict == REJECTED
        assert score is not None

    def test_an_unknown_policy_is_rejected_at_construction(self, tmp_path):
        with pytest.raises(ValueError):
            short_gate(tmp_path, short_policy="sometimes")
