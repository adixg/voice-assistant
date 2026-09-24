"""voicepipe/utterances.py and tools/voiceprint_add.py -- keeping real Stick
audio for the voiceprint, without ever letting a wrong clip in by accident."""
import importlib.util
import os
import sys
import wave

import pytest

from voicepipe import utterances

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("voiceprint_add", os.path.join(ROOT, "tools", "voiceprint_add.py"))
voiceprint_add = importlib.util.module_from_spec(spec)
sys.modules["voiceprint_add"] = voiceprint_add
spec.loader.exec_module(voiceprint_add)


def make_wav(path, seconds=3.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(16000 * seconds))
    return str(path)


class TestKeep:
    def test_names_the_copy_with_verdict_and_score(self, tmp_path):
        src = make_wav(tmp_path / "in.wav")
        dest = utterances.keep(src, str(tmp_path / "kept"), "accepted", 0.61234)
        assert os.path.basename(dest).endswith("_accepted_0.612.wav")

    def test_unscored_clip_is_marked_none(self, tmp_path):
        src = make_wav(tmp_path / "in.wav")
        assert utterances.keep(src, str(tmp_path / "k"), "too_short", None).endswith("_too_short_none.wav")

    def test_two_in_the_same_second_do_not_overwrite(self, tmp_path):
        src = make_wav(tmp_path / "in.wav")
        a = utterances.keep(src, str(tmp_path / "k"), "accepted", 0.7)
        b = utterances.keep(src, str(tmp_path / "k"), "accepted", 0.7)
        assert a != b and len(os.listdir(tmp_path / "k")) == 2

    def test_only_the_newest_max_keep_survive(self, tmp_path):
        d = tmp_path / "k"
        d.mkdir()
        for i in range(6):
            (d / f"2026092{i}-000000_accepted_0.700.wav").write_bytes(b"x")
        utterances.prune(str(d), 4)
        assert sorted(os.listdir(d)) == [f"2026092{i}-000000_accepted_0.700.wav" for i in range(2, 6)]

    def test_an_unwritable_directory_returns_none_instead_of_raising(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("not a directory")
        assert utterances.keep(make_wav(tmp_path / "in.wav"), str(blocker / "sub"), "accepted", 0.7) is None


class FakeBackend:
    name = "wespeaker-ecapa-tdnn512"

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, path):
        return self.vectors[os.path.basename(path)]


class TestVoiceprintAdd:
    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        from voicepipe.speaker import Voiceprint
        vp = Voiceprint([[1.0, 0.0]], path=str(tmp_path / "voiceprint.json"), backend=FakeBackend.name)
        vp.save()
        clips = tmp_path / "clips"
        clips.mkdir()
        vectors = {"mine.wav": [0.8, 0.6], "stranger.wav": [0.0, 1.0], "short.wav": [0.9, 0.436]}
        make_wav(clips / "mine.wav", 3)
        make_wav(clips / "stranger.wav", 3)
        make_wav(clips / "short.wav", 1.0)
        monkeypatch.setattr(voiceprint_add, "load", lambda: (Voiceprint.load(vp.path), FakeBackend(vectors)))
        monkeypatch.setattr("voicepipe.speaker.SAMPLES_DIR", str(tmp_path / "samples"))
        return vp.path, str(clips), Voiceprint

    def run_add(self, clips_dir, *names, force=False):
        voiceprint_add.main(["--dir", clips_dir, "add", *names] + (["--force"] if force else []))

    def test_appends_a_clip_that_sounds_like_the_owner_and_keeps_the_old_samples(self, setup):
        path, clips, Voiceprint = setup
        self.run_add(clips, "mine.wav")
        vp = Voiceprint.load(path)
        assert len(vp) == 2 and vp.samples[0] == [1.0, 0.0]
        assert any(f.endswith(".bak") for f in os.listdir(os.path.dirname(path)))

    def test_refuses_a_clip_that_does_not_sound_like_the_owner(self, setup, capsys):
        path, clips, Voiceprint = setup
        self.run_add(clips, "stranger.wav")
        assert len(Voiceprint.load(path)) == 1
        assert "REFUSED" in capsys.readouterr().out

    def test_refuses_a_clip_under_two_seconds(self, setup, capsys):
        path, clips, Voiceprint = setup
        self.run_add(clips, "short.wav")
        assert len(Voiceprint.load(path)) == 1
        assert "under 2.0s" in capsys.readouterr().out

    def test_a_clip_already_in_the_voiceprint_is_not_added_twice(self, setup, capsys):
        path, clips, Voiceprint = setup
        self.run_add(clips, "mine.wav", "mine.wav")
        self.run_add(clips, "mine.wav")
        assert len(Voiceprint.load(path)) == 2
        assert "already in the voiceprint" in capsys.readouterr().out

    def test_force_overrides_the_refusal(self, setup):
        path, clips, Voiceprint = setup
        self.run_add(clips, "stranger.wav", force=True)
        assert len(Voiceprint.load(path)) == 2

    def test_apply_does_nothing_without_yes(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(voiceprint_add.subprocess, "run", lambda *a, **k: calls.append(a))
        voiceprint_add.main(["apply"])
        assert calls == [] and "--yes" in capsys.readouterr().out

    def test_gate_tag_reads_verdict_and_score_from_the_name(self):
        assert voiceprint_add.gate_tag("20260924-101500_rejected_0.536.wav") == ("rejected", "0.536")
