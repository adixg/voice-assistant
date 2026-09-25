"""voicepipe/conversation_log.py: one JSON line and one recording per turn, with
the recordings (never the transcripts) pruned oldest-first past a size cap."""
import json

from voicepipe import conversation_log


def test_record_appends_a_line_and_keeps_the_recording(tmp_path):
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 100)
    for text in ("one", "two"):
        turn = conversation_log.new_turn()
        turn["heard"] = text
        conversation_log.record(str(tmp_path / "log"), turn, str(wav))

    [log] = list((tmp_path / "log").glob("*.jsonl"))
    turns = [json.loads(line) for line in log.read_text().splitlines()]
    assert [t["heard"] for t in turns] == ["one", "two"]
    assert all((tmp_path / "log" / t["audio"]).exists() for t in turns)
    assert log.name == f"{turns[0]['id'][:4]}-{turns[0]['id'][4:6]}-{turns[0]['id'][6:8]}.jsonl"


def test_oldest_recordings_are_pruned_past_the_cap_and_transcripts_kept(tmp_path):
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"\0" * 1000)
    ids = []
    for i in range(5):
        turn = {"id": f"20260925-1200{i:02d}-000", "time": "t"}
        conversation_log.record(str(tmp_path / "log"), turn, str(wav), max_audio_bytes=2500)
        ids.append(turn["id"])

    kept = sorted(p.stem for p in (tmp_path / "log" / "audio").iterdir())
    assert kept == ids[-2:]
    lines = (tmp_path / "log" / "2026-09-25.jsonl").read_text().splitlines()
    assert len(lines) == 5


def test_a_failure_never_raises(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    conversation_log.record(str(blocker), conversation_log.new_turn())
    assert "couldn't log the turn" in capsys.readouterr().out
