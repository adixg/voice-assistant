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


def one_line(text):
    """Collapse newlines: the worker protocols below are line-oriented, so a
    reply containing a newline would otherwise be read as two requests."""
    return " ".join(text.split())


def strip_think(text):
    """Drop a reasoning model's <think> block, keeping only the reply.

    Not specific to any one backend — qwen3, the Hermes models, r1 and others
    all emit this, whichever server is in front of them. Only the closing tag
    is required, since a model can be mid-block when thinking is disabled
    server-side and never emits the opener.
    """
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()
