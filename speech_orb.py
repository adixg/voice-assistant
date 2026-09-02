#!/usr/bin/env python3
"""An animated "speech orb" for the voice assistant — a glowing blob that idles,
listens, thinks and speaks.  Pure PySide6 + QPainter, no image assets.

Use it from code:

    from speech_orb import SpeechOrb
    orb = SpeechOrb()                 # a QWidget
    orb.set_state("listening")        # idle | listening | thinking | speaking
    orb.push_level(0.7)               # 0..1 audio amplitude, call ~30-60x/sec

Or run it standalone for a demo with buttons and a live microphone:

    python speech_orb.py                 # normal window
    python speech_orb.py --frameless     # translucent, draggable, always-on-top
"""
from __future__ import annotations

import array
import math
import sys

from PySide6.QtCore import Qt, QTimer, QPointF, QElapsedTimer
from PySide6.QtGui import QColor, QRadialGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QCheckBox
)

# per-state look: core colour, glow colour, how strongly audio deforms the rim,
# and the idle "breathing" depth
STATES = {
    "idle":      dict(core="#4aa3ff", glow="#1e5cff", reactivity=0.05, breathe=0.06),
    "listening": dict(core="#37e0a0", glow="#12b981", reactivity=0.85, breathe=0.05),
    "thinking":  dict(core="#ffb038", glow="#ff7a3d", reactivity=0.12, breathe=0.10),
    "speaking":  dict(core="#ff6ca8", glow="#ff3d6e", reactivity=1.00, breathe=0.05),
}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(_lerp(a.red(),   b.red(),   t)),
        round(_lerp(a.green(), b.green(), t)),
        round(_lerp(a.blue(),  b.blue(),  t)),
        round(_lerp(a.alpha(), b.alpha(), t)),
    )


class SpeechOrb(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._state = "idle"
        self._cfg = STATES["idle"]
        self._core = QColor(self._cfg["core"])
        self._glow = QColor(self._cfg["glow"])
        self._core_target = QColor(self._cfg["core"])
        self._glow_target = QColor(self._cfg["glow"])

        self._level = 0.0          # smoothed amplitude actually drawn
        self._level_target = 0.0   # most recent push_level(), decays on its own
        self._spin = 0.0           # rotation for the "thinking" ring

        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)      # ~60 fps

    # ---- public API ---------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; pick one of {list(STATES)}")
        self._state = state
        self._cfg = STATES[state]
        self._core_target = QColor(self._cfg["core"])
        self._glow_target = QColor(self._cfg["glow"])

    def state(self) -> str:
        return self._state

    def push_level(self, level: float) -> None:
        self._level_target = max(0.0, min(1.0, float(level)))

    # ---- animation --------------------------------------------------------
    def _tick(self) -> None:
        # amplitude: fast attack, slow release; the target also bleeds away so a
        # stale value doesn't keep the orb inflated
        self._level_target *= 0.90
        follow = 0.5 if self._level_target > self._level else 0.12
        self._level = _lerp(self._level, self._level_target, follow)

        self._core = _lerp_color(self._core, self._core_target, 0.08)
        self._glow = _lerp_color(self._glow, self._glow_target, 0.08)
        self._spin = (self._spin + 2.2) % 360
        self.update()

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        t = self._clock.elapsed() / 1000.0
        base = min(w, h) * 0.30
        breathe = self._cfg["breathe"] * math.sin(t * 1.6)
        react = self._cfg["reactivity"] * self._level
        radius = base * (1.0 + breathe + 0.35 * react)

        # ---- outer glow: a few soft, growing haloes
        for i, (scale, alpha) in enumerate(((2.4, 26), (1.8, 40), (1.35, 70))):
            g = QRadialGradient(QPointF(cx, cy), radius * scale)
            c = QColor(self._glow)
            c.setAlpha(alpha + int(40 * react))
            g.setColorAt(0.0, c)
            edge = QColor(self._glow)
            edge.setAlpha(0)
            g.setColorAt(1.0, edge)
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), radius * scale, radius * scale)

        # ---- wobbling body: radius modulated by a couple of rotating harmonics
        path = QPainterPath()
        lobes_amp = (0.015 + 0.12 * react) * base
        n = 96
        for k in range(n + 1):
            ang = 2 * math.pi * k / n
            r = (radius
                 + lobes_amp * math.sin(3 * ang + t * 1.7)
                 + lobes_amp * 0.6 * math.sin(5 * ang - t * 2.3))
            pt = QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang))
            path.moveTo(pt) if k == 0 else path.lineTo(pt)
        path.closeSubpath()

        body = QRadialGradient(QPointF(cx - radius * 0.3, cy - radius * 0.35), radius * 1.6)
        hi = QColor(self._core).lighter(135)
        body.setColorAt(0.0, hi)
        body.setColorAt(0.55, QColor(self._core))
        body.setColorAt(1.0, QColor(self._core).darker(160))
        p.setBrush(body)
        p.setPen(Qt.NoPen)
        p.drawPath(path)

        # ---- specular highlight
        spec = QRadialGradient(QPointF(cx - radius * 0.35, cy - radius * 0.4), radius * 0.9)
        white = QColor(255, 255, 255, 150)
        spec.setColorAt(0.0, white)
        spec.setColorAt(0.4, QColor(255, 255, 255, 30))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(QPointF(cx - radius * 0.28, cy - radius * 0.32),
                      radius * 0.55, radius * 0.55)

        # ---- "thinking": a rotating dashed ring
        if self._state == "thinking":
            pen = QPen(QColor(255, 255, 255, 180), max(2.0, base * 0.03))
            pen.setDashPattern([1.0, 3.5])
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.save()
            p.translate(cx, cy)
            p.rotate(self._spin)
            rr = radius * 1.22
            p.drawEllipse(QPointF(0, 0), rr, rr)
            p.restore()

        p.end()

    # ---- frameless-mode dragging --------------------------------------------
    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if getattr(self, "_drag", None) and e.buttons() & Qt.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)


# --------------------------------------------------------------------------- demo
class MicMeter:
    """Feeds the default microphone's RMS level into an orb. Optional."""

    def __init__(self, orb: SpeechOrb, gain: float = 6.0):
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
        self.orb = orb
        self.gain = gain
        fmt = QAudioFormat()
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        dev = QMediaDevices.defaultAudioInput()
        if dev.isNull():
            raise RuntimeError("no default audio input")
        self.src = QAudioSource(dev, fmt)
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


def _demo(frameless: bool) -> int:
    app = QApplication(sys.argv)
    win = QWidget()
    win.setWindowTitle("speech orb")
    win.resize(420, 480)

    orb = SpeechOrb()
    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(orb, 1)

    mic_holder = {}

    if frameless:
        win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        win.setAttribute(Qt.WA_TranslucentBackground)
        win.resize(260, 260)
    else:
        win.setStyleSheet("background:#0e1116;")
        row = QHBoxLayout()
        for name in STATES:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: orb.set_state(n))
            b.setStyleSheet("color:#ddd;background:#232833;border:1px solid #333;"
                            "padding:6px 10px;border-radius:6px;")
            row.addWidget(b)
        layout.addLayout(row)

        mic = QCheckBox("use microphone (drives the level live)")
        mic.setStyleSheet("color:#bbb;padding:6px;")

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

        # when the mic is off, fake some amplitude while "speaking"/"listening"
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
