#!/usr/bin/env python3
"""The desktop face for the voice assistant — the same pixel-art character,
palette, animation and effects the M5StickS3 shows, rendered in a window.

This is a port of firmware/m5stick_bridge/src/main.cpp's drawing code, not a
lookalike: it draws into the Stick's own 240x135 logical canvas, using the
sprites tools/make_face_sprites.py extracted (assets/sprites/, the same crops
that are baked into sprites.h), then scales that up with nearest-neighbour so
the pixels stay pixels. The constants below are the firmware's constants, and
the effects are the firmware's effects — so what you see here is what the
Stick shows, larger.

Use it from code:

    from speech_orb import SpeechOrb
    orb = SpeechOrb()                 # a QWidget
    orb.set_state("listening")        # idle | listening | thinking | speaking
    orb.push_level(0.7)               # 0..1 audio amplitude, call ~30-60x/sec
    orb.set_caption("hello there")    # transcript or reply, like the Stick's

Or run it standalone for a demo with buttons and a live microphone:

    python speech_orb.py                 # normal window
    python speech_orb.py --frameless     # translucent, draggable, always-on-top

If the window is blank, the sprites haven't been extracted yet:

    python tools/make_face_sprites.py
"""
from __future__ import annotations

import array
import math
import os
import random
import sys

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPolygon
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SPRITE_DIR = os.path.join(HERE, "assets", "sprites")

# --------------------------------------------------------------- palette
# Catppuccin Macchiato, the same accents main.cpp assigns to the same states.
MACCHIATO = dict(
    base="#24273a", crust="#181926", text="#cad3f5", subtext0="#a5adcb",
    blue="#8aadf4", sapphire="#7dc4e4", sky="#91d7e3", teal="#8bd5ca",
    green="#a6da95", yellow="#eed49f", peach="#f5a97f", maroon="#ee99a0",
    red="#ed8796", mauve="#c6a0f6", pink="#f5bde6", lavender="#b7bdf8",
    rosewater="#f4dbd6",
)
BASE_BG = MACCHIATO["base"]

COL_BASE = QColor(MACCHIATO["base"])
COL_TEXT = QColor(MACCHIATO["text"])
COL_GREEN = QColor(MACCHIATO["green"])      # listening
COL_MAUVE = QColor(MACCHIATO["mauve"])      # thinking
COL_YELLOW = QColor(MACCHIATO["yellow"])    # speaking
COL_PEACH = QColor(MACCHIATO["peach"])
COL_SAPPHIRE = QColor(MACCHIATO["sapphire"])
COL_LAVENDER = QColor(MACCHIATO["lavender"])

# --------------------------------------------------------------- geometry
# main.cpp's canvas and layout constants, unchanged — see the comments there
# for why each is what it is.
CANVAS_W, CANVAS_H = 240, 135
CHAR_TOP = 2
TEXT_BAND_Y = 110
HEADPHONE_CY = CHAR_TOP + 63
CAPTION_Y = 111
CAPTION_LINE_H = 8
MAX_CAPTION_LINES = 3
MAX_CAPTION_CHARS = 36

SPRITE_W, SPRITE_H = 220, 116        # kSpriteW / kSpriteH
IDLE_ANIM_W, IDLE_ANIM_H = 128, 128  # kIdleAnimW / kIdleAnimH

FRAME_MS = 90  # main.cpp's maybeDrawFace() interval, ~11fps

# --------------------------------------------------------------- idle gestures
BREATHE_FRAME_MS = 550
GESTURE_FRAME_MS = 110
GESTURE_FRAMES = 4  # each row in IDLE_ANIM_ROWS keeps 4 curated frames

# Weighted exactly as main.cpp's pickGesture(): blink dominates, the rest are
# occasional flourishes rather than a tic.
GESTURE_WEIGHTS = [("blink", 55), ("look", 15), ("smile", 12), ("yawn", 10), ("sleepy", 8)]

# --------------------------------------------------------------- waveform bars
HIST_N = 14
BARS, BAR_W, BAR_STEP = 5, 6, 8
BAR_NEAR_EDGE = 55  # clear of her earcups

# --------------------------------------------------------------- particles
MAX_PARTICLES = 8
FLANK_MARGIN = 46  # spawn x stays within this of the left/right edge

# (count, colours, min/max life ms, min/max respawn ms, drift, squares,
#  avoid the headphone band, y range) — main.cpp's profileFor().
PROFILES = {
    "idle":      (8, (COL_SAPPHIRE, COL_LAVENDER), 2600, 4200, 500, 2500, False, False, False),
    "listening": (3, (COL_GREEN, COL_SAPPHIRE),    1800, 2800, 800, 2000, False, False, True),
    "thinking":  (8, (COL_MAUVE,),                 1400, 2400, 300, 1200, True,  True,  False),
    "speaking":  (3, (COL_YELLOW, COL_PEACH),      1800, 2800, 800, 2000, False, False, True),
}
PARTICLE_Y_LO, PARTICLE_Y_HI = 8, 100

STATES = ("idle", "listening", "thinking", "speaking")


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    """main.cpp's lerp565(): fade a particle up from the background colour."""
    return QColor(round(_lerp(a.red(), b.red(), t)),
                  round(_lerp(a.green(), b.green(), t)),
                  round(_lerp(a.blue(), b.blue(), t)))


def load_sprites(directory=SPRITE_DIR):
    """The extracted pixel art, by sprite name. Empty if it hasn't been
    generated yet — the orb then draws everything except her."""
    sprites = {}
    if not os.path.isdir(directory):
        return sprites
    for entry in os.listdir(directory):
        if entry.endswith(".png"):
            image = QImage(os.path.join(directory, entry))
            if not image.isNull():
                sprites[entry[:-4]] = image
    return sprites


class Particle:
    """One member of the shared pool. See main.cpp's particle-field comment:
    a triangular brightness envelope drives both the glyph shape and a fade up
    from the background, so each one blooms blank -> dot -> cross -> star and
    back down before respawning somewhere new."""

    __slots__ = ("active", "x", "y", "vx", "vy", "born_at", "life_ms", "respawn_at", "color", "square")

    def __init__(self):
        self.active = False
        self.x = self.y = self.vx = self.vy = 0.0
        self.born_at = self.life_ms = self.respawn_at = 0
        self.color = COL_SAPPHIRE
        self.square = False


class SpeechOrb(QWidget):
    """The Stick's screen, in a window."""

    def __init__(self, parent=None, sprite_dir=SPRITE_DIR):
        super().__init__(parent)
        self.setMinimumSize(CANVAS_W * 2, CANVAS_H * 2)
        self.setFocusPolicy(Qt.StrongFocus)
        self.on_enter = None  # callback(str) — Enter/Space while focused

        self.sprites = load_sprites(sprite_dir)
        self._missing_warned = False

        self._state = "idle"
        self._caption = ""
        self._level = 0.0         # smoothed amplitude actually drawn
        self._level_target = 0.0  # most recent push_level(), decays on its own
        self._hist = [0.0] * HIST_N
        self._hist_pos = 0

        # idle gesture state machine (main.cpp's idleAnimSprite)
        self._gesture = None
        self._gesture_start = 0
        self._next_gesture_at = 4000  # a few seconds of breathing before the first

        self._particles = [Particle() for _ in range(MAX_PARTICLES)]
        self._particle_state = None  # forces a (re)init on the first real frame
        self._last_particle_ms = 0

        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FRAME_MS)

    # ---- public API -------------------------------------------------------
    def set_state(self, state):
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; pick one of {list(STATES)}")
        self._state = state

    def state(self):
        return self._state

    def push_level(self, level):
        self._level_target = max(0.0, min(1.0, float(level)))

    def set_caption(self, text):
        """The transcript while listening, or the reply while speaking — the
        only text the Stick shows, and the only text shown here."""
        self._caption = text or ""

    # ---- animation --------------------------------------------------------
    def _tick(self):
        # fast attack, slow release; the target also bleeds away so a stale
        # value doesn't keep the level pinned high
        self._level_target *= 0.90
        follow = 0.5 if self._level_target > self._level else 0.12
        self._level = _lerp(self._level, self._level_target, follow)

        self._hist_pos = (self._hist_pos + 1) % HIST_N
        self._hist[self._hist_pos] = self._level
        self.update()

    # ---- sprite selection -------------------------------------------------
    def _idle_sprite_name(self, t):
        """main.cpp's idleAnimSprite(): a slow breathing loop interrupted every
        few seconds by one randomly chosen gesture, which plays once through
        and hands back to breathing."""
        if self._gesture is None:
            if t < self._next_gesture_at:
                return f"idle_breathe{(t // BREATHE_FRAME_MS) % GESTURE_FRAMES}"
            self._gesture = random.choices([g for g, _ in GESTURE_WEIGHTS],
                                           [w for _, w in GESTURE_WEIGHTS])[0]
            self._gesture_start = t

        index = (t - self._gesture_start) // GESTURE_FRAME_MS
        if index >= GESTURE_FRAMES:  # finished — breathe again, schedule the next
            self._gesture = None
            self._next_gesture_at = t + 3500 + random.randrange(5000)
            return "idle_breathe0"
        return f"idle_{self._gesture}{index}"

    def _character_sprite(self, t):
        """(image, width, height) for the current state, or None if the sprites
        haven't been extracted."""
        if self._state == "idle":
            name, w, h = self._idle_sprite_name(t), IDLE_ANIM_W, IDLE_ANIM_H
        else:
            w, h = SPRITE_W, SPRITE_H
            if self._state == "speaking":
                # mouth shape follows the TTS audio's live amplitude
                level = self._level
                name = ("mouth_wide" if level > 0.45 else
                        "mouth_medium" if level > 0.25 else
                        "mouth_small" if level > 0.08 else "mouth_closed")
            else:
                name = self._state
        image = self.sprites.get(name)
        return (image, w, h) if image is not None else None

    # ---- painting ---------------------------------------------------------
    def paintEvent(self, _event):
        # Draw the Stick's own 240x135 canvas, then blit it up whole. Doing it
        # this way (rather than scaling each coordinate) is what keeps this a
        # port rather than a reimplementation: every number below is the
        # firmware's number, in the firmware's pixel space.
        canvas = QImage(CANVAS_W, CANVAS_H, QImage.Format_RGB32)
        canvas.fill(COL_BASE)

        c = QPainter(canvas)
        c.setPen(Qt.NoPen)
        t = self._clock.elapsed()

        self._draw_character(c, t)
        # solid banner under her, so the caption always sits on clean background
        c.fillRect(0, TEXT_BAND_Y, CANVAS_W, CANVAS_H - TEXT_BAND_Y, COL_BASE)
        self._draw_amplitude_pulses(c)
        self._draw_particles(c, t)
        self._draw_caption(c)
        c.end()

        # integer-ish nearest-neighbour upscale, letterboxed, pixels kept sharp
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.fillRect(self.rect(), COL_BASE)
        scale = min(self.width() / CANVAS_W, self.height() / CANVAS_H)
        w, h = CANVAS_W * scale, CANVAS_H * scale
        p.drawImage(QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), canvas)
        p.end()

    def _draw_character(self, c, t):
        found = self._character_sprite(t)
        if found is None:
            self._draw_missing_sprites_notice(c)
            return
        image, w, h = found
        c.drawImage((CANVAS_W - w) // 2, CHAR_TOP, image)

    def _draw_missing_sprites_notice(self, c):
        if not self._missing_warned:
            print(f"  (speech orb: no sprites in {SPRITE_DIR} — "
                  f"run `python tools/make_face_sprites.py`)", flush=True)
            self._missing_warned = True
        c.setPen(QColor(MACCHIATO["subtext0"]))
        font = QFont()
        font.setPixelSize(9)
        c.setFont(font)
        c.drawText(QRectF(0, 40, CANVAS_W, 30), Qt.AlignCenter,
                   "no sprites — run\ntools/make_face_sprites.py")
        c.setPen(Qt.NoPen)

    def _draw_amplitude_pulses(self, c):
        """Live amplitude flanking her at headphone height: green while she's
        hearing you, yellow while she's talking."""
        if self._state == "listening":
            color = COL_GREEN
        elif self._state == "speaking":
            color = COL_YELLOW
        else:
            return
        for i in range(BARS):
            idx = (self._hist_pos + HIST_N - 1 - i) % HIST_N  # newest nearest her
            bar_h = 4 + int(self._hist[idx] * 40)
            x = BAR_NEAR_EDGE - BAR_W - i * BAR_STEP
            y = HEADPHONE_CY - bar_h // 2
            c.setBrush(color)
            c.drawRoundedRect(QRectF(x, y, BAR_W, bar_h), 2, 2)
            c.drawRoundedRect(QRectF(CANVAS_W - x - BAR_W, y, BAR_W, bar_h), 2, 2)

    # ---- the particle field ----------------------------------------------
    def _spawn(self, particle, profile, now, immediate):
        (_, colors, min_life, max_life, _, max_respawn, drift, squares, avoid) = profile
        local = 6 + random.random() * (FLANK_MARGIN - 6)
        particle.x = local if random.random() < 0.5 else CANVAS_W - local
        y = PARTICLE_Y_LO + random.random() * (PARTICLE_Y_HI - PARTICLE_Y_LO)
        if avoid:  # keep clear of the waveform bars' band
            lo, hi = HEADPHONE_CY - 22, HEADPHONE_CY + 22
            if lo < y < hi:
                y = lo - 6 if y < HEADPHONE_CY else hi + 6
        particle.y = y
        particle.vx = (random.randrange(200) - 100) / 4000.0 if drift else 0.0
        particle.vy = (random.randrange(200) - 100) / 6000.0 if drift else 0.0
        particle.color = random.choice(colors)
        particle.square = squares and random.random() < 0.5
        particle.life_ms = random.randint(min_life, max_life)
        particle.born_at = now
        if immediate:
            particle.active = True
            particle.respawn_at = 0
        else:  # stagger a freshly-entered state instead of popping every slot on
            particle.active = False
            particle.respawn_at = now + random.randrange(max_respawn)

    def _draw_particles(self, c, now):
        profile = PROFILES.get(self._state)
        if profile is None:
            self._particle_state = None
            return
        count, _, _, _, min_respawn, max_respawn, drift, _, _ = profile

        if self._state != self._particle_state:
            self._particle_state = self._state
            self._last_particle_ms = 0  # no huge drift step from a stale timestamp
            for p in self._particles:
                p.active = False
            for p in self._particles[:count]:
                self._spawn(p, profile, now, immediate=False)
        dt = (now - self._last_particle_ms) if self._last_particle_ms else 0
        self._last_particle_ms = now

        for p in self._particles[:count]:
            if not p.active:
                if now >= p.respawn_at:
                    self._spawn(p, profile, now, immediate=True)
                continue
            age = now - p.born_at
            if age >= p.life_ms:
                p.active = False
                p.respawn_at = now + random.randint(min_respawn, max_respawn)
                continue
            if drift and dt:
                p.x += p.vx * dt
                p.y += p.vy * dt
                # bounce back toward its own edge rather than crossing her body
                if FLANK_MARGIN < p.x < CANVAS_W - FLANK_MARGIN:
                    p.vx = -p.vx
                if p.y < PARTICLE_Y_LO or p.y > PARTICLE_Y_HI:
                    p.vy = -p.vy

            env = 1.0 - abs(2.0 * (age / p.life_ms) - 1.0)  # triangular, peaks mid-life
            if env <= 0.06:
                continue  # effectively blank
            color = _lerp_color(COL_BASE, p.color, env)
            if p.square:
                self._glyph_square(c, p.x, p.y, color)
            elif env < 0.28:
                self._glyph_dot(c, p.x, p.y, color)
            elif env < 0.62:
                self._glyph_cross(c, p.x, p.y, color)
            else:
                self._glyph_star(c, p.x, p.y, color)

    # ---- glyphs (main.cpp's drawGlyph cases the particle field uses) ------
    @staticmethod
    def _glyph_dot(c, x, y, color):
        c.setBrush(color)
        c.drawEllipse(QRectF(x - 3, y - 3, 6, 6))

    @staticmethod
    def _glyph_cross(c, x, y, color):
        c.fillRect(QRectF(x - 3, y, 7, 1), color)
        c.fillRect(QRectF(x, y - 3, 1, 7), color)

    @staticmethod
    def _glyph_star(c, x, y, color):
        c.setBrush(color)
        c.drawPolygon(QPolygon([QPoint(int(x), int(y) - 5), QPoint(int(x) - 5, int(y)),
                                QPoint(int(x), int(y) + 5), QPoint(int(x) + 5, int(y))]))
        c.fillRect(QRectF(x - 5, y, 11, 1), color)
        c.fillRect(QRectF(x, y - 5, 1, 11), color)

    @staticmethod
    def _glyph_square(c, x, y, color):
        c.fillRect(QRectF(x - 1, y - 1, 3, 3), color)

    # ---- caption ----------------------------------------------------------
    def _draw_caption(self, c):
        if not self._caption:
            return
        c.setPen(COL_TEXT)
        font = QFont()
        font.setPixelSize(7)
        c.setFont(font)
        for i, line in enumerate(wrap_caption(self._caption)):
            c.drawText(QRectF(2, CAPTION_Y + i * CAPTION_LINE_H, CANVAS_W - 4, CAPTION_LINE_H),
                       Qt.AlignHCenter | Qt.AlignVCenter, line)
        c.setPen(Qt.NoPen)

    # ---- input ------------------------------------------------------------
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and callable(self.on_enter):
            self.on_enter("")
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        self.setFocus()
        self._drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if getattr(self, "_drag", None) and e.buttons() & Qt.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)


def wrap_caption(text, max_chars=MAX_CAPTION_CHARS, max_lines=MAX_CAPTION_LINES):
    """Greedy word wrap into at most `max_lines` lines, matching main.cpp's
    drawCaptionText(). A reply too long to fit gets an ellipsis on the last
    line, so it reads as truncated rather than as having simply stopped."""
    # Hard-split any word too long to ever fit a line, so the packing loop
    # below can assume every word fits on its own.
    words = []
    for word in text.split():
        while len(word) > max_chars:
            words.append(word[:max_chars])
            word = word[max_chars:]
        if word:
            words.append(word)

    lines, current, dropped = [], "", 0
    for i, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        lines.append(current)  # never empty: a lone word always fits by now
        if len(lines) == max_lines:
            dropped = len(words) - i
            current = ""
            break
        current = word
    if current:
        lines.append(current)
    if dropped:
        lines[-1] = lines[-1][:max_chars - 1].rstrip() + "…"
    return lines


# --------------------------------------------------------------------------- demo
class MicMeter:
    """Feeds the default microphone's RMS level into an orb. Optional."""

    def __init__(self, orb, gain=6.0):
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
        self.orb = orb
        self.gain = gain
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise RuntimeError("no default audio input")
        self.src = QAudioSource(device, fmt)
        self.io = self.src.start()
        self.io.readyRead.connect(self._read)

    def _read(self):
        raw = bytes(self.io.readAll())
        if len(raw) < 2:
            return
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
        if not samples:
            return
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
        self.orb.push_level(min(1.0, rms * self.gain))

    def stop(self):
        self.src.stop()


def _demo(frameless):
    app = QApplication(sys.argv)
    win = QWidget()
    win.setWindowTitle("Rina")
    win.resize(CANVAS_W * 3, CANVAS_H * 3 + 90)

    orb = SpeechOrb()
    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(orb, 1)

    mic_holder = {}

    if frameless:
        win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        win.resize(CANVAS_W * 2, CANVAS_H * 2)
    else:
        win.setStyleSheet(f"background:{BASE_BG};")
        row = QHBoxLayout()
        for name in STATES:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: (
                orb.set_state(n),
                orb.set_caption({"listening": "tell me about your day",
                                 "thinking": "tell me about your day",
                                 "speaking": "it was good, thanks for asking"}.get(n, "")),
            ))
            b.setStyleSheet(
                f"color:{MACCHIATO['text']};background:{MACCHIATO['crust']};"
                f"border:1px solid {MACCHIATO['blue']};padding:6px 10px;border-radius:6px;"
            )
            row.addWidget(b)
        layout.addLayout(row)

        mic = QCheckBox("use microphone (drives the level live)")
        mic.setStyleSheet(f"color:{MACCHIATO['subtext0']};padding:6px;")

        def toggle_mic(on):
            if on:
                try:
                    mic_holder["m"] = MicMeter(orb)
                except Exception as e:  # noqa: BLE001
                    mic.setText(f"mic unavailable: {e}")
                    mic.setChecked(False)
            else:
                m = mic_holder.pop("m", None)
                if m:
                    m.stop()

        mic.toggled.connect(toggle_mic)
        layout.addWidget(mic)

        # with the mic off, fake some amplitude so the bars and mouth move
        fake = QTimer(win)
        fake.timeout.connect(lambda: (
            not mic.isChecked()
            and orb.state() in ("speaking", "listening")
            and orb.push_level(0.35 + 0.4 * abs(math.sin(orb._clock.elapsed() / 140.0)))
        ))
        fake.start(40)

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_demo("--frameless" in sys.argv))
