"""voicepipe.personas — the built-in system prompts, the --persona / --system
precedence the entrypoints share, and the user profile folded in on top."""
from voicepipe.personas import (
    DEFAULT_PERSONA, PERSONAS, PROFILE_HEADER, compose, load_profile, resolve,
)


class TestPersonas:
    def test_default_persona_exists(self):
        assert DEFAULT_PERSONA in PERSONAS

    def test_every_persona_is_a_non_empty_prompt(self):
        assert PERSONAS and all(p.strip() for p in PERSONAS.values())

    def test_every_persona_keeps_replies_short(self):
        """The Stick's caption is three lines; a persona that rambles overflows
        it and makes every spoken reply long."""
        assert all("short sentence" in p and "words" in p for p in PERSONAS.values())

    def test_every_persona_bans_stage_directions_and_emojis(self):
        """Both get read aloud by the TTS, which sounds absurd."""
        for name, prompt in PERSONAS.items():
            assert "stage direction" in prompt, name
            assert "emoji" in prompt, name

    def test_the_angry_persona_is_irritable_not_abusive(self):
        assert "not cruel" in PERSONAS["angry"]


class TestResolve:
    def test_no_override_uses_the_named_persona(self):
        assert resolve("assistant") == PERSONAS["assistant"]

    def test_an_override_wins(self):
        assert resolve("partner", "you are a pirate") == "you are a pirate"

    def test_an_empty_override_means_send_no_system_prompt(self):
        """Distinct from None: --system '' is how you hand the conversation to
        a model that already carries its own persona, like the rina Modelfile."""
        assert resolve("partner", "") == ""

    def test_none_is_not_treated_as_an_override(self):
        assert resolve("partner", None) == PERSONAS["partner"]


class TestLoadProfile:
    """The about-me file is read fresh each start, so editing it takes effect
    without a rebuild — and an unfilled template must contribute nothing."""

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_profile(tmp_path / "nope.md") is None

    def test_empty_file_yields_nothing(self, tmp_path):
        p = tmp_path / "about.md"
        p.write_text("   \n\n  ")
        assert load_profile(p) is None

    def test_html_comments_are_stripped(self, tmp_path):
        p = tmp_path / "about.md"
        p.write_text("Name: Aditya\n<!-- fill this in -->\nTimezone: EST")
        loaded = load_profile(p)
        assert "fill this in" not in loaded
        assert "Aditya" in loaded and "EST" in loaded

    def test_a_template_with_only_comments_yields_nothing(self, tmp_path):
        """Shipping the unfilled template must not silently spend context on
        section headers that say nothing."""
        p = tmp_path / "about.md"
        p.write_text("<!-- e.g. what you do -->\n<!-- another hint -->\n")
        assert load_profile(p) is None

    def test_real_content_survives(self, tmp_path):
        p = tmp_path / "about.md"
        p.write_text("# About me\n\n- Name: Aditya\n")
        assert "Aditya" in load_profile(p)

    def test_oversized_profile_warns_but_still_loads(self, tmp_path, capsys):
        p = tmp_path / "about.md"
        p.write_text("x" * 5000)
        loaded = load_profile(p)
        assert loaded is not None
        assert "context" in capsys.readouterr().out


class TestCompose:
    def test_no_profile_leaves_the_prompt_alone(self):
        assert compose("be nice", None) == "be nice"

    def test_profile_is_appended_under_a_header(self):
        result = compose("be nice", "Name: Aditya")
        assert result.startswith("be nice")
        assert PROFILE_HEADER in result
        assert result.endswith("Name: Aditya")

    def test_profile_alone_still_works_with_an_empty_system_prompt(self):
        """--system '' means the model carries its own persona; the profile
        should still reach it."""
        result = compose("", "Name: Aditya")
        assert "Name: Aditya" in result
        assert PROFILE_HEADER in result

    def test_neither_yields_an_empty_prompt(self):
        assert compose("", None) == ""


def test_partner_never_promises_a_lookup_it_does_not_make():
    """She said "Let me see..." about the weather and never checked (2026-09-24)."""
    from voicepipe.personas import PARTNER
    assert "later" in PARTNER and "tool" in PARTNER
