"""Reminders the gateway speaks through the Stick when they come due.

The gateway owns them (one writer, no file races): the agent's MCP tools go
through its control API, and it checks for due ones every few seconds. They
live in one JSON file, rewritten atomically on every change, so they survive
a restart and can be read by hand.

A reminder is one-off or repeats daily, on weekdays, or weekly, at the same
local wall-clock time (so 9:00 stays 9:00 across a DST change). One that
comes due while no Stick is connected stays due, and is said as soon as one
connects; a repeating one then moves to its next time in the future, so a
Stick that was off for three days doesn't hear the same reminder three times.
"""
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

REPEATS = ("none", "daily", "weekdays", "weekly")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MAX_REMINDERS = 50
MAX_TEXT = 200
# Said late by more than this, the reminder says when it was for.
LATE_AFTER = timedelta(minutes=3)


class ReminderError(ValueError):
    """A request that can't become a reminder (bad time, bad repeat, too many)."""


class ReminderStore:
    def __init__(self, path, tz="America/New_York"):
        self.path = path
        self.tz = ZoneInfo(tz)
        self.reminders = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            # A broken file must not stop the gateway; say so and keep it
            # aside rather than overwrite it on the next change.
            print(f"  ! reminders file unreadable ({e}); starting empty", flush=True)
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            return []
        return [r for r in data.get("reminders", []) if isinstance(r, dict) and "due" in r]

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".reminders-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"reminders": self.reminders}, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- times -------------------------------------------------------------

    def now(self):
        return datetime.now(timezone.utc).astimezone(self.tz)

    def parse_when(self, at=None, in_minutes=None, now=None):
        """`at` is a local "HH:MM" (the next time the clock shows it), a day
        and a time ("tomorrow 09:30", "monday 09:30"), or "YYYY-MM-DDTHH:MM";
        `in_minutes` is relative. Exactly one of them.

        The day words exist because the model doesn't know today's date: asked
        for "tomorrow at 9:30" it wrote 2023-10-03T09:30 (2026-09-25). For the
        same reason every error says what the time is now."""
        now = now or self.now()
        try:
            return self._parse_when(at, in_minutes, now)
        except ReminderError as e:
            raise ReminderError(f"{e} (it is now {now.strftime('%A')} {now.date().isoformat()} "
                                f"{now.strftime('%H:%M')})") from None

    def _parse_when(self, at, in_minutes, now):
        if (at is None) == (in_minutes is None):
            raise ReminderError("give exactly one of at or in_minutes")
        if in_minutes is not None:
            if not isinstance(in_minutes, (int, float)) or isinstance(in_minutes, bool) \
                    or not 0 < in_minutes <= 60 * 24 * 366:
                raise ReminderError("in_minutes must be a positive number of minutes, up to a year")
            return (now + timedelta(minutes=in_minutes)).replace(microsecond=0)
        if not isinstance(at, str):
            raise ReminderError("at must be a string")
        text = at.strip().lower()
        clock = re.fullmatch(r"(?:(today|tomorrow|[a-z]+day)\s+)?(\d{1,2}):(\d{2})", text)
        if clock:
            day, hour, minute = clock[1], int(clock[2]), int(clock[3])
            if hour > 23 or minute > 59:
                raise ReminderError(f"not a time of day: {at}")
            if day is None:
                due = self._local(now.date(), hour, minute)
                return due if due > now else self._local(now.date() + timedelta(days=1), hour, minute)
            if day in ("today", "tomorrow"):
                due = self._local(now.date() + timedelta(days=day == "tomorrow"), hour, minute)
                if due <= now:
                    raise ReminderError(f"{self.describe_time(due, now)} is in the past")
                return due
            if day not in WEEKDAYS:
                raise ReminderError(f"not a day: {clock[1]}")
            # The next such day, a week on if that time today has passed.
            ahead = (WEEKDAYS.index(day) - now.weekday()) % 7
            due = self._local(now.date() + timedelta(days=ahead), hour, minute)
            return due if due > now else due + timedelta(days=7)
        try:
            due = datetime.fromisoformat(text)
        except ValueError:
            raise ReminderError(f"at must look like 15:00, tomorrow 15:00, monday 15:00 or "
                                f"2026-09-26T15:00, not {at!r}") from None
        due = due.replace(tzinfo=self.tz) if due.tzinfo is None else due.astimezone(self.tz)
        if due <= now - timedelta(minutes=1):
            raise ReminderError(f"{self.describe_time(due, now)} is in the past")
        return due.replace(microsecond=0)

    def _local(self, day, hour, minute):
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    def next_time(self, reminder, after):
        """The repeat's next local occurrence strictly after `after`, or None."""
        repeat = reminder.get("repeat", "none")
        if repeat == "none":
            return None
        due = datetime.fromisoformat(reminder["due"]).astimezone(self.tz)
        day, step = due.date(), 7 if repeat == "weekly" else 1
        while True:
            day += timedelta(days=step)
            if repeat == "weekdays" and day.weekday() >= 5:
                continue
            candidate = self._local(day, due.hour, due.minute)
            if candidate > after:
                return candidate

    def describe_time(self, when, now=None):
        """"3:00 PM today", "9:30 AM tomorrow", "Monday 28 September 8:00 AM"."""
        now = now or self.now()
        when = when.astimezone(self.tz)
        clock = when.strftime("%I:%M %p").lstrip("0")
        days = (when.date() - now.date()).days
        if days == 0:
            return f"{clock} today"
        if days == 1:
            return f"{clock} tomorrow"
        return f"{when.strftime('%A')} {when.day} {when.strftime('%B')} {clock}"

    def describe(self, reminder, now=None):
        return dict(reminder, when=self.describe_time(datetime.fromisoformat(reminder["due"]), now))

    # -- changes -----------------------------------------------------------

    def add(self, text, at=None, in_minutes=None, repeat="none", now=None):
        now = now or self.now()
        if not isinstance(text, str) or not text.strip():
            raise ReminderError("a reminder needs some text")
        if repeat not in REPEATS:
            raise ReminderError(f"repeat must be one of {', '.join(REPEATS)}")
        if len(self.reminders) >= MAX_REMINDERS:
            raise ReminderError(f"there are already {MAX_REMINDERS} reminders; cancel some first")
        due = self.parse_when(at, in_minutes, now)
        if repeat == "weekdays" and due.weekday() >= 5:
            due = self.next_time({"due": due.isoformat(), "repeat": "weekdays"}, due)
        reminder = {"id": uuid.uuid4().hex[:6], "text": " ".join(text.split())[:MAX_TEXT],
                    "due": due.isoformat(), "repeat": repeat,
                    "created": now.isoformat(timespec="seconds")}
        self.reminders.append(reminder)
        self._save()
        return self.describe(reminder, now)

    def list(self, now=None):
        ordered = sorted(self.reminders, key=lambda r: datetime.fromisoformat(r["due"]))
        return [self.describe(r, now) for r in ordered]

    def cancel(self, reminder_id):
        for reminder in self.reminders:
            if reminder["id"] == reminder_id:
                self.reminders.remove(reminder)
                self._save()
                return reminder
        raise KeyError(reminder_id)

    def due(self, now=None):
        now = now or self.now()
        return [r for r in self.list(now) if datetime.fromisoformat(r["due"]) <= now]

    def spoken_line(self, reminder, now=None):
        """What to say: "Reminder: stretch", or, when it's late, when it was for."""
        now = now or self.now()
        due = datetime.fromisoformat(reminder["due"])
        if now - due > LATE_AFTER:
            return f"Reminder from {self.describe_time(due, now)}: {reminder['text']}"
        return f"Reminder: {reminder['text']}"

    def delivered(self, reminder_id, now=None):
        """Said: a one-off one is done, a repeating one moves to its next time."""
        now = now or self.now()
        for reminder in self.reminders:
            if reminder["id"] == reminder_id:
                following = self.next_time(reminder, now)
                if following is None:
                    self.reminders.remove(reminder)
                else:
                    reminder["due"] = following.isoformat()
                self._save()
                return
