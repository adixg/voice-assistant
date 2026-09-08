"""The lines Rina says unprompted, to be encouraging.

Deliberately a static list rather than a generated one. The alternative —
asking the model for a fresh line every time — costs a full LLM turn every
quarter of an hour, keeps a chat model or an agent warm for no reason, and can
fail or wander off-persona while nobody is watching. A curated list always
works, is instant, needs no GPU, and reads exactly as intended.

`{owner}` is filled in from voicepipe.speaker.OWNER, so the name lives in one
place and isn't spelled out twice.
"""
import random

from .speaker import OWNER

# She uses the honorific about a third of the time — every single line ending
# in "-senpai" stops landing as affection and starts sounding like a script.
LINES = [
    "You've got this!",
    "I believe in you, {owner}-senpai!",
    "Whatever you're working on right now, keep going. You're closer than you think.",
    "Hey. You're doing better than you're giving yourself credit for.",
    "{owner}-senpai, don't forget to drink some water. That's an order.",
    "I'm proud of you, you know. Just thought you should hear it.",
    "One thing at a time. That's all it ever takes.",
    "Stretch! Look away from the screen for a second. I'll wait.",
    "You've solved harder things than this before.",
    "Fighting! You can do it, {owner}-senpai!",
    "If it's not working yet, that just means you're not finished yet.",
    "I've seen you push through worse. Keep at it.",
    "Take a breath. Then take the next step.",
    "You're allowed to take a break, senpai. You'll think better after one.",
    "Whatever happens today, I'm on your side.",
    "Small progress is still progress. Don't discount it.",
    "You're not behind. You're just in the middle.",
    "{owner}-senpai! Posture check. Sit up.",
    "Hard things are hard. That's not a sign you're doing it wrong.",
    "I know you'll figure it out. You always do.",
    "Don't forget to eat something. I'm serious.",
    "You showed up today, and that already counts for something.",
    "Almost there. Keep going, senpai.",
    "You make it look easy, but I know it isn't. Well done.",
]

# How many recent lines are off-limits. Hearing the same encouragement twice
# in half an hour makes the whole thing feel mechanical, which is exactly the
# opposite of the point.
NO_REPEAT_WINDOW = 8

_recent = []


def line(owner=OWNER):
    """One encouraging line, avoiding anything said in the last few."""
    choices = [t for t in LINES if t not in _recent] or list(LINES)
    chosen = random.choice(choices)
    _recent.append(chosen)
    del _recent[:-NO_REPEAT_WINDOW]
    return chosen.format(owner=owner)


def reset():
    """Forget the recent history (tests, and a fresh run)."""
    _recent.clear()
