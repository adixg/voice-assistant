"""speech_orb.py's pure logic — caption wrapping and the idle-gesture state
machine, which are ports of main.cpp's drawCaptionText() and idleAnimSprite()
and have to keep matching them.

No QWidget is constructed here (no display, no Qt event loop): these exercise
the module-level helpers and the gesture selection through a bare instance.
"""
import pytest

pytest.importorskip("PySide6", reason="the orb is optional; chat_loop runs headless without it")

import speech_orb
from speech_orb import (
    GESTURE_FRAMES, GESTURE_WEIGHTS, MAX_CAPTION_CHARS, MAX_CAPTION_LINES,
    PROFILES, STATES, wrap_caption,
)


class TestWrapCaption:
    def test_short_text_is_one_line(self):
        assert wrap_caption("hello there") == ["hello there"]

    def test_empty_text_yields_no_lines(self):
        assert wrap_caption("") == []

    def test_wraps_on_word_boundaries(self):
        lines = wrap_caption("word " * 20)
        assert len(lines) > 1
        assert all(not line.startswith(" ") for line in lines)

    def test_never_exceeds_the_line_length(self):
        for line in wrap_caption("supercalifragilistic expialidocious " * 4):
            assert len(line) <= MAX_CAPTION_CHARS

    def test_never_exceeds_the_line_count(self):
        assert len(wrap_caption("word " * 200)) <= MAX_CAPTION_LINES

    def test_overlong_text_is_ellipsised_rather_than_cut_silently(self):
        lines = wrap_caption("word " * 200)
        assert lines[-1].endswith("…")

    def test_text_that_just_fits_is_not_ellipsised(self):
        lines = wrap_caption("one two three")
        assert not lines[-1].endswith("…")

    def test_a_word_longer_than_a_line_is_split_not_dropped(self):
        lines = wrap_caption("x" * (MAX_CAPTION_CHARS * 2))
        assert all(len(line) <= MAX_CAPTION_CHARS for line in lines)
        assert "".join(line.rstrip("…") for line in lines).count("x") > MAX_CAPTION_CHARS

    def test_no_empty_lines_are_produced(self):
        for text in ("a b c", "y" * 100, "word " * 50, "one"):
            assert all(line for line in wrap_caption(text))


class TestIdleGestures:
    """A slow breathing loop, interrupted every few seconds by one gesture that
    plays through once and hands back to breathing."""

    def _orb(self):
        return speech_orb.SpeechOrb.__new__(speech_orb.SpeechOrb)  # no Qt init needed

    def _fresh(self):
        orb = self._orb()
        orb._gesture = None
        orb._gesture_start = 0
        orb._next_gesture_at = 4000
        return orb

    def test_breathes_before_the_first_gesture(self):
        orb = self._fresh()
        assert orb._idle_sprite_name(0).startswith("idle_breathe")

    def test_breathing_cycles_through_its_frames(self):
        orb = self._fresh()
        names = {orb._idle_sprite_name(t) for t in range(0, 4000, 200)}
        assert names == {f"idle_breathe{i}" for i in range(GESTURE_FRAMES)}

    def test_a_gesture_starts_once_the_deadline_passes(self):
        orb = self._fresh()
        name = orb._idle_sprite_name(4000)
        assert not name.startswith("idle_breathe")
        assert orb._gesture is not None

    def test_a_gesture_plays_through_then_returns_to_breathing(self):
        orb = self._fresh()
        orb._idle_sprite_name(4000)
        gesture = orb._gesture
        seen = [orb._idle_sprite_name(4000 + i * speech_orb.GESTURE_FRAME_MS)
                for i in range(GESTURE_FRAMES)]

        assert seen == [f"idle_{gesture}{i}" for i in range(GESTURE_FRAMES)]

        after = orb._idle_sprite_name(4000 + GESTURE_FRAMES * speech_orb.GESTURE_FRAME_MS)
        assert after.startswith("idle_breathe")
        assert orb._gesture is None

    def test_the_next_gesture_is_scheduled_a_few_seconds_out(self):
        orb = self._fresh()
        orb._idle_sprite_name(4000)
        end = 4000 + GESTURE_FRAMES * speech_orb.GESTURE_FRAME_MS
        orb._idle_sprite_name(end)

        assert end + 3500 <= orb._next_gesture_at <= end + 8500

    def test_every_gesture_it_can_pick_has_sprites_on_disk(self):
        """A weight naming a gesture with no extracted frames would show as a
        blank character for a moment, not an error."""
        sprites = speech_orb.load_sprites()
        if not sprites:
            pytest.skip("sprites not extracted; run tools/make_face_sprites.py")
        for gesture, _ in GESTURE_WEIGHTS:
            for i in range(GESTURE_FRAMES):
                assert f"idle_{gesture}{i}" in sprites
        for i in range(GESTURE_FRAMES):
            assert f"idle_breathe{i}" in sprites


class TestParticleProfiles:
    def test_every_animated_state_has_a_profile(self):
        assert set(PROFILES) == set(STATES)

    def test_no_profile_asks_for_more_particles_than_the_pool_holds(self):
        for name, profile in PROFILES.items():
            assert profile[0] <= speech_orb.MAX_PARTICLES, name

    def test_life_and_respawn_ranges_are_ordered(self):
        for name, (_, _, min_life, max_life, min_re, max_re, *_) in PROFILES.items():
            assert min_life < max_life, name
            assert min_re < max_re, name

    def test_listening_and_speaking_keep_clear_of_the_waveform_bars(self):
        """Those two states draw the amplitude bars at headphone height, so
        their particles have to avoid that band or they collide."""
        assert PROFILES["listening"][8] is True
        assert PROFILES["speaking"][8] is True
        assert PROFILES["idle"][8] is False


class TestSpritesMatchTheFirmware:
    """The orb and the Stick read the same extraction, so a sprite the
    firmware draws must exist here too."""

    def test_the_states_the_orb_draws_have_sprites(self):
        sprites = speech_orb.load_sprites()
        if not sprites:
            pytest.skip("sprites not extracted; run tools/make_face_sprites.py")
        for name in ("listening", "thinking", "idle",
                     "mouth_closed", "mouth_small", "mouth_medium", "mouth_wide"):
            assert name in sprites

    def test_sprite_sizes_match_the_firmware_constants(self):
        sprites = speech_orb.load_sprites()
        if not sprites:
            pytest.skip("sprites not extracted; run tools/make_face_sprites.py")
        assert sprites["listening"].width() == speech_orb.SPRITE_W
        assert sprites["listening"].height() == speech_orb.SPRITE_H
        assert sprites["idle_breathe0"].width() == speech_orb.IDLE_ANIM_W
        assert sprites["idle_breathe0"].height() == speech_orb.IDLE_ANIM_H

    def test_the_character_fits_the_canvas(self):
        assert speech_orb.SPRITE_W <= speech_orb.CANVAS_W
        assert speech_orb.CHAR_TOP + speech_orb.SPRITE_H <= speech_orb.CANVAS_H
