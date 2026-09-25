"""voicepipe/reminders.py: parsing when a reminder is for, repeating at the
same wall-clock time, and never repeating a missed one more than once."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from voicepipe.reminders import ReminderError, ReminderStore

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 25, 18, 0, tzinfo=NY)  # a Friday


@pytest.fixture
def store(tmp_path):
    return ReminderStore(str(tmp_path / "reminders.json"))


def test_a_clock_time_means_the_next_time_the_clock_shows_it(store):
    assert store.parse_when(at="19:30", now=NOW) == datetime(2026, 9, 25, 19, 30, tzinfo=NY)
    assert store.parse_when(at="9:00", now=NOW) == datetime(2026, 9, 26, 9, 0, tzinfo=NY)


def test_day_words_so_the_model_never_needs_the_date(store):
    assert store.parse_when(at="tomorrow 09:30", now=NOW) == datetime(2026, 9, 26, 9, 30, tzinfo=NY)
    assert store.parse_when(at="Today 19:00", now=NOW) == datetime(2026, 9, 25, 19, 0, tzinfo=NY)
    assert store.parse_when(at="monday 8:00", now=NOW) == datetime(2026, 9, 28, 8, 0, tzinfo=NY)
    # Friday 17:00 has passed on this Friday: next week's.
    assert store.parse_when(at="friday 17:00", now=NOW) == datetime(2026, 10, 2, 17, 0, tzinfo=NY)
    for at in ("today 17:00", "someday 10:00", "funday 10:00"):
        with pytest.raises(ReminderError):
            store.parse_when(at=at, now=NOW)


def test_errors_say_what_the_date_is_so_a_retry_can_get_it_right(store):
    """The model wrote 2023-10-03T09:30 for "tomorrow at 9:30" (2026-09-25)."""
    with pytest.raises(ReminderError, match=r"in the past \(it is now Friday 2026-09-25 18:00\)"):
        store.parse_when(at="2023-10-03T09:30", now=NOW)


def test_dates_minutes_and_bad_input(store):
    assert store.parse_when(at="2026-09-28T08:15", now=NOW) == datetime(2026, 9, 28, 8, 15, tzinfo=NY)
    assert (store.parse_when(in_minutes=20, now=NOW) - NOW).total_seconds() == 1200
    for kwargs in ({}, {"at": "15:00", "in_minutes": 5}, {"at": "25:00"}, {"at": "tomorrow"},
                   {"at": "2026-09-24T10:00"}, {"in_minutes": 0}, {"in_minutes": True}):
        with pytest.raises(ReminderError):
            store.parse_when(now=NOW, **kwargs)


def test_add_saves_to_the_file_and_a_new_store_reads_it_back(store):
    reminder = store.add("  call   the dentist ", at="19:00", now=NOW)
    assert reminder["text"] == "call the dentist" and reminder["when"] == "7:00 PM today"
    again = ReminderStore(store.path)
    assert [r["id"] for r in again.list(NOW)] == [reminder["id"]]
    with pytest.raises(ReminderError):
        store.add("x", at="19:00", repeat="hourly", now=NOW)


def test_due_one_off_reminders_are_said_once(store):
    reminder = store.add("stretch", in_minutes=1, now=NOW)
    later = datetime(2026, 9, 25, 18, 2, tzinfo=NY)
    assert store.due(NOW) == [] and [r["id"] for r in store.due(later)] == [reminder["id"]]
    assert store.spoken_line(reminder, later) == "Reminder: stretch"
    store.delivered(reminder["id"], later)
    assert store.list(later) == [] and store.due(later) == []


def test_a_late_reminder_says_when_it_was_for(store):
    reminder = store.add("take the bins out", at="18:30", now=NOW)
    later = datetime(2026, 9, 25, 21, 0, tzinfo=NY)
    assert store.spoken_line(reminder, later) == "Reminder from 6:30 PM today: take the bins out"


def test_repeats_keep_the_wall_clock_time_and_skip_missed_days(store):
    daily = store.add("pills", at="09:00", repeat="daily", now=NOW)  # Saturday 9:00
    # The Stick was off all weekend: said once on Monday morning, next is Tuesday.
    monday = datetime(2026, 9, 28, 9, 5, tzinfo=NY)
    store.delivered(daily["id"], monday)
    assert store.list(monday)[0]["due"] == "2026-09-29T09:00:00-04:00"
    # Across the DST change (1 November 2026), 9:00 stays 9:00.
    store.reminders[0]["due"] = "2026-10-31T09:00:00-04:00"
    store.delivered(daily["id"], datetime(2026, 10, 31, 9, 0, tzinfo=NY))
    assert store.list()[0]["due"] == "2026-11-01T09:00:00-05:00"


def test_weekdays_skip_the_weekend_even_when_set_on_one(store):
    reminder = store.add("standup", at="10:00", repeat="weekdays", now=NOW)
    assert reminder["due"].startswith("2026-09-28T10:00")  # Saturday -> Monday
    store.delivered(reminder["id"], datetime(2026, 9, 28, 10, 0, tzinfo=NY))
    assert store.list()[0]["due"].startswith("2026-09-29T10:00")


def test_cancel_and_a_broken_file_is_kept_aside(store, tmp_path):
    reminder = store.add("x", at="19:00", now=NOW)
    assert store.cancel(reminder["id"])["text"] == "x"
    with pytest.raises(KeyError):
        store.cancel(reminder["id"])
    assert json.loads((tmp_path / "reminders.json").read_text()) == {"reminders": []}
    (tmp_path / "reminders.json").write_text("{not json")
    assert ReminderStore(store.path).reminders == []
    assert (tmp_path / "reminders.json.broken").exists()
