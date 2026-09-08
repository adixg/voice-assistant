"""voicepipe.text — the sentence-aware chunking every TTS backend shares
(pure string logic; no model, no torch)."""
from voicepipe.text import chunks, one_line


class TestChunks:
    def test_empty_text_yields_one_empty_chunk(self):
        assert chunks("") == [""]

    def test_whitespace_only_yields_one_empty_chunk(self):
        assert chunks("   \n  ") == [""]

    def test_short_single_sentence_is_one_chunk(self):
        assert chunks("Hello there.") == ["Hello there."]

    def test_no_terminal_punctuation_still_one_chunk(self):
        assert chunks("just a fragment with no period") == ["just a fragment with no period"]

    def test_short_sentences_merge_into_one_chunk(self):
        result = chunks("Hi. How are you? I'm fine.", limit=200)
        assert len(result) == 1
        assert result[0] == "Hi. How are you? I'm fine."

    def test_sentences_split_across_chunks_once_limit_exceeded(self):
        # three ~40-char sentences with a limit that only fits two at a time
        sent = "This is a sentence of some length."  # 35 chars
        text = " ".join([sent] * 3)
        result = chunks(text, limit=75)
        assert len(result) > 1
        assert all(len(c) <= 75 for c in result)
        # nothing lost: every sentence's text appears somewhere in the output
        assert "".join(result).count("This is a sentence") == 3

    def test_long_sentence_is_hard_wrapped_at_word_boundaries(self):
        text = "word " * 60  # 300 chars, no sentence punctuation at all
        result = chunks(text.strip() + ".", limit=50)
        assert len(result) > 1
        for c in result:
            assert len(c) <= 50
            # hard-wrap cuts on whitespace, so no chunk should end mid-word
            # (the only exception is a single "word" longer than the limit,
            # not the case here)
            assert not c.endswith(" ")

    def test_japanese_terminal_punctuation_splits_sentences(self):
        # the sentence-boundary regex needs whitespace *after* the punctuation
        # to split there (matches how VITS input is normally spaced); each
        # sentence alone fits under the limit, but both together don't
        result = chunks("こんにちは。 元気ですか。", limit=10)
        assert result == ["こんにちは。", "元気ですか。"]

    def test_japanese_punctuation_without_a_space_does_not_split(self):
        # no whitespace after "。" -> the regex has no split point, so this
        # stays one "sentence" and gets hard-wrapped by length instead
        result = chunks("こんにちは。元気ですか。", limit=3)
        assert len(result) > 1
        assert "".join(result) == "こんにちは。元気ですか。"

    def test_default_limit_is_200(self):
        text = "word " * 5  # well under 200 chars
        assert chunks(text) == [text.strip()]


class TestOneLine:
    """The worker protocols are line-oriented, so a newline inside a reply
    would otherwise be read as the start of a second request."""

    def test_newlines_become_spaces(self):
        assert one_line("two\nlines") == "two lines"

    def test_runs_of_whitespace_collapse(self):
        assert one_line("a  \n\t b") == "a b"

    def test_leading_and_trailing_whitespace_is_dropped(self):
        assert one_line("  padded\n") == "padded"

    def test_plain_text_is_unchanged(self):
        assert one_line("nothing to do here") == "nothing to do here"
