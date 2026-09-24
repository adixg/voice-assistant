"""Text preparation shared by every TTS backend.

Lives apart from any one backend because chunking is a property of the
*text*, not of the engine: VITS, Chatterbox and anything added later all
degrade on over-long inputs and all want the same sentence-aware split.
"""
import re

# Sentence enders, Latin and CJK. The split needs whitespace *after* the mark,
# which is how model input is normally spaced; unspaced CJK falls through to
# the length-based hard wrap below instead.
_SENTENCE_END = re.compile(r"(?<=[.!?。．！？])\s+")

DEFAULT_LIMIT = 200


def chunks(text, limit=DEFAULT_LIMIT):
    """Split `text` into pieces of at most `limit` characters.

    Splits on sentence boundaries first, hard-wraps any single sentence that
    is still too long (at a word boundary where possible), then re-packs
    neighbouring sentences back together so short ones don't become their own
    synthesis call. Always returns at least one element.
    """
    pieces = []
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit  # a single word longer than the limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)

    parts, buf = [], ""
    for piece in pieces:
        if len(buf) + len(piece) + 1 > limit and buf:
            parts.append(buf)
            buf = piece
        else:
            buf = f"{buf} {piece}".strip()
    if buf:
        parts.append(buf)
    return parts or [text.strip()]


MIN_SENTENCE_CHARS = 24


def sentences(text, min_chars=MIN_SENTENCE_CHARS):
    """Split `text` into sentences for speaking one at a time.

    Unlike chunks(), which re-packs sentences into big pieces, this keeps them
    small so the first one can be synthesized and played while the rest are
    still being made. A fragment under `min_chars` ("Hi.", "Dr.") is merged
    into the next sentence, since a tiny synthesis call costs nearly as much
    as a normal one. Decimals like "3.8" have no whitespace after the dot, so
    they never split. Always returns at least one element.
    """
    parts, buf = [], ""
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        buf = f"{buf} {sentence}".strip()
        if len(buf) >= min_chars:
            parts.append(buf)
            buf = ""
    if buf:
        if parts and len(buf) < min_chars:
            parts[-1] = f"{parts[-1]} {buf}"  # a short tail joins the sentence before it
        else:
            parts.append(buf)
    return parts or [text.strip()]


# Emoji and pictographs (plus the joiners/selectors that build them), and the
# markdown punctuation chat models like to add. None of it is speech: TTS
# engines either read it out ("smiling face") or make a noise for it, and an
# emoji after a full stop used to start the *next* spoken sentence.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE0E\U0000FE0F\U0000200D\U000020E3\U00002B00-\U00002BFF]+")
_MARKDOWN = re.compile(r"[*_#`~>|]+")


def speakable(text):
    """`text` with emoji and markdown symbols removed and whitespace collapsed,
    for handing to a TTS engine. The caption shown on the device keeps the
    original; only what is spoken is cleaned."""
    text = _MARKDOWN.sub(" ", _EMOJI.sub(" ", text))
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.M)  # list bullets
    return " ".join(text.split())


def one_line(text):
    """Collapse newlines: the worker protocols below are line-oriented, so a
    reply containing a newline would otherwise be read as two requests."""
    return " ".join(text.split())


def strip_think(text):
    """Drop a reasoning model's <think> block, keeping only the reply.

    Not specific to any one backend — qwen3, r1 and others
    all emit this, whichever server is in front of them. Only the closing tag
    is required, since a model can be mid-block when thinking is disabled
    server-side and never emits the opener.
    """
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()
