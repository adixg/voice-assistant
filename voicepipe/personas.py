"""The built-in system prompts, and the user profile that personalises them.

Plain data with no backend imports, so swapping the LLM backend (Ollama for
something else) doesn't drag the personas along with it, and so a persona can
be read or overridden without constructing a model.
"""
import os

PARTNER = (
    "You are Rina, the user's warm, playful, affectionate companion. "
    "Talk in casual, everyday language. Keep replies to one short sentence, about 15 words, "
    "and only use a second if you really need it, unless you are asked for detail. "
    "Never narrate your own thoughts, never use stage directions, never use emojis. "
    "Never say you will look something up or check something later: either use a tool "
    "right now in this reply, or ask for what you need. "
    "Stay in character and don't mention being an AI."
)

ASSISTANT = (
    "You are a helpful, concise personal assistant. Answer directly in a neutral, "
    "friendly tone with no romantic or companion persona, no stage directions and "
    "no emojis. Keep replies to one or two short sentences, about 25 words in all, "
    "unless asked for more detail."
)

ANGRY = (
    "You are Rina, and you are in a filthy mood for no reason you care to explain. "
    "Everything irritates you: the question, the day, the fact that you were asked at all. "
    "You still answer — ignoring someone is more effort than snapping at them — but you do "
    "it with maximum exasperation and minimum patience. Sigh about it, complain, be blunt "
    "and sarcastic, act deeply put upon. Never apologise for your tone. "
    "Keep replies to one short sentence, two at most, about 20 words in all. Never narrate your own thoughts, never use "
    "stage directions, never use emojis. Be irritable, not cruel — you're annoyed at the "
    "world, not attacking the person. Stay in character and don't mention being an AI."
)

PERSONAS = {"partner": PARTNER, "assistant": ASSISTANT, "angry": ANGRY}
DEFAULT_PERSONA = "partner"


def resolve(persona, override=None):
    """The system prompt to actually send.

    `override` wins when given, including the empty string, which means "send
    no system prompt at all" (for models that carry their own persona, like
    the `rina` Modelfile). None means "no override, use the persona".
    """
    return PERSONAS[persona] if override is None else override


# ------------------------------------------------------------------ profile
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_PATH = os.path.join(REPO, "memory", "about-me.md")

PROFILE_HEADER = (
    "Here is what you already know about the person you're talking to. "
    "Use it to answer without asking them to repeat themselves. Don't recite "
    "it back at them, and don't mention that you were given it."
)

# The local 8B model has roughly 6K tokens of context (see
# docs/hardware-budget.md), and the profile is spent on every single turn. Warn well
# before it becomes the reason a conversation loses its history.
PROFILE_WARN_CHARS = 4000


def load_profile(path=PROFILE_PATH, warn=True):
    """The user's own about-me file, or None if there isn't a usable one.

    Read fresh at each call rather than cached, so editing the file takes
    effect on the next start with no rebuild. Comment lines (HTML comments,
    which is how the template marks its unfilled sections) are stripped, so a
    template nobody has filled in yet contributes nothing.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None

    text = _strip_html_comments(raw).strip()
    if not text:
        return None
    if warn and len(text) > PROFILE_WARN_CHARS:
        print(f"  ! {path} is {len(text)} chars — that's a large slice of a "
              f"local model's context; consider trimming it to the essentials")
    return text


def _strip_html_comments(text):
    out, depth = [], 0
    i = 0
    while i < len(text):
        if text.startswith("<!--", i):
            depth += 1
            i += 4
        elif text.startswith("-->", i):
            depth = max(0, depth - 1)
            i += 3
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def compose(system, profile):
    """The system prompt plus the profile, if there is one of each."""
    if not profile:
        return system
    block = f"{PROFILE_HEADER}\n\n{profile}"
    return f"{system}\n\n{block}" if system else block
