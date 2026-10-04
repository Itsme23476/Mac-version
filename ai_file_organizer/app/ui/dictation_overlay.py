"""Reactive voice-animation overlay for dictation (VoiceOS / Wispr Flow style).

A small frameless floating pill that animates while the user speaks. Drops into
the dictation controller via three methods:

    overlay.show_state("listening" | "transcribing" | "done")
    overlay.set_level(0.0 .. 1.0)   # live mic amplitude
    overlay.hide()

Everything is drawn in paintEvent with QPainter; a single 60fps QTimer advances
the animation phase, eases bar heights toward their targets, and repaints. The
timer stops whenever the overlay is hidden so it costs nothing at rest.

Live preview:
    /Users/damianosmalliaros/Desktop/Mac_app/venv/bin/python \
        ai_file_organizer/app/ui/dictation_overlay.py
"""

import math

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QGuiApplication, QColor, QPainter, QPainterPath, QLinearGradient, QPen, QBrush, QFont
)
from PySide6.QtWidgets import QWidget


# --- geometry -----------------------------------------------------------------
PILL_W, PILL_H = 240, 68          # visible capsule
GLOW_MARGIN = 26                  # extra room around the pill for the outer glow
WIDGET_W = PILL_W + GLOW_MARGIN * 2
WIDGET_H = PILL_H + GLOW_MARGIN * 2
CORNER = 20
BOTTOM_GAP = 80                   # px above the bottom edge of the screen

# --- look ---------------------------------------------------------------------
PILL_BG = QColor(10, 10, 18, 235)          # #0A0A12 ~92% opacity
BORDER = QColor(255, 255, 255, 28)
PURPLE = QColor("#7C4DFF")                 # brand purple (both modes; matches the app + popup)
PURPLE_LIGHT = QColor("#B39DFF")

# --- bars ---------------------------------------------------------------------
N_BARS = 9
BAR_W = 5
BAR_GAP = 8
BAR_MIN_H = 6
BAR_MAX_H = 40
EASE = 0.25                                # lerp factor toward target each frame

STATE_LISTENING = "listening"
STATE_TRANSCRIBING = "transcribing"
STATE_DONE = "done"


class DictationOverlay(QWidget):
    """Frameless, always-on-top, non-focus-stealing voice animation pill."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(WIDGET_W, WIDGET_H)
        self.setWindowTitle("Filect Voice")  # lets _configure_macos find the NSWindow by title

        # Keep this HUD on the system font (its fixed size is tuned to it). Style-only.
        self.setStyleSheet("QWidget { font-family: '-apple-system', 'SF Pro Display', "
                           "'Helvetica Neue', 'Segoe UI', sans-serif; }")

        self._state = STATE_LISTENING
        self._latched = False                    # hands-free latch (double-tap) vs push-to-talk
        self._mode = "dictate"                   # "dictate" | "search" — drives accent color
        self._phase = 0.0                       # advances every frame, drives motion
        self._level_target = 0.0                # set by set_level, clamped 0..1
        self._level = 0.0                        # eased value the paint loop follows
        self._heights = [0.0] * N_BARS           # eased bar heights, 0..1
        self._done_progress = 0.0                # 0..1, checkmark draw-in + fade

        self._timer = QTimer(self)
        self._timer.setInterval(16)              # ~60fps
        self._timer.timeout.connect(self._tick)

        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self.hide)

    def set_mode(self, mode: str) -> None:
        """Mark the pill's mode so it's visually distinguishable while you hold:
        'search' = magnifying glass, 'organize' = folder, 'dictate' = plain."""
        self._mode = mode if mode in ("search", "organize") else "dictate"

    def set_latched(self, latched: bool) -> None:
        """Toggle the hands-free 'latched' look while listening: a persistent
        pulsing ring + an ∞ glyph so the user sees it's locked on and needn't
        hold the key. False returns to the normal push-to-talk listening look."""
        self._latched = bool(latched)
        self.update()

    def _accent(self) -> QColor:
        return PURPLE          # both modes use brand purple (matches the results popup)

    def _accent_light(self) -> QColor:
        return PURPLE_LIGHT    # search mode is distinguished by the 🔍 icon, not color

    # -- public API ------------------------------------------------------------
    def show_state(self, state: str) -> None:
        """Show the overlay in one of: listening, transcribing, done."""
        if state not in (STATE_LISTENING, STATE_TRANSCRIBING, STATE_DONE):
            state = STATE_LISTENING
        self._state = state
        self.setWindowOpacity(1.0)
        self._done_timer.stop()

        if state == STATE_DONE:
            self._done_progress = 0.0
            self._done_timer.start(600)          # fade the check, then hide()

        self._reposition()
        first_show = not self.isVisible()
        if first_show:
            self.show()
        # macOS: float over ANY app / fullscreen regardless of which app is focused.
        # Qt.Tool windows otherwise auto-hide when the app deactivates; Qt also resets
        # these props during show(), so re-apply on short delays (like quick-search).
        # ONLY on first show — re-running this on a state change (listening->transcribing)
        # does heavy CGS space/level work on the UI thread and visibly stutters the pill
        # right at key-release. Once shown, the panel is already configured on the right
        # Space, so state changes just repaint.
        import sys
        if sys.platform == "darwin" and first_show:
            self._configure_macos()
            QTimer.singleShot(10, self._configure_macos)
            QTimer.singleShot(60, self._configure_macos)
        # NOTE: no self.raise_() here — it activates Filect and yanks focus off the
        # user's app. orderFrontRegardless() in _configure_macos already floats the
        # non-activating panel to the front without stealing focus.
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def set_level(self, level: float) -> None:
        """Feed live mic amplitude (0.0..1.0). Values are clamped."""
        self._level_target = max(0.0, min(1.0, float(level)))

    def hide(self) -> None:
        self._timer.stop()
        self._done_timer.stop()
        super().hide()

    # -- positioning -----------------------------------------------------------
    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + geo.height() - self.height() - BOTTOM_GAP
        self.move(x, y)

    # -- macOS overlay behavior ------------------------------------------------
    def _configure_macos(self) -> None:
        """Float over all Spaces / fullscreen apps and don't hide when Filect isn't
        the active app — the same NSPanel config the quick-search overlay uses."""
        try:
            from AppKit import NSApp
        except Exception:
            return
        try:
            ns_window = None
            for w in NSApp.windows():
                try:
                    if w.title() == "Filect Voice":
                        ns_window = w
                        break
                except Exception:
                    continue
            if ns_window is None:
                return
            ns_window.setCollectionBehavior_((1 << 0) | (1 << 8))  # AllSpaces | FullScreenAuxiliary
            ns_window.setLevel_(1000)                               # NSScreenSaverWindowLevel
            # Instant show/hide — macOS otherwise fades a panel out over ~100ms, which
            # read as a dead "empty pill" frame between the dots and the text landing.
            if hasattr(ns_window, "setAnimationBehavior_"):
                try:
                    ns_window.setAnimationBehavior_(2)             # NSWindowAnimationBehaviorNone
                except Exception:
                    pass
            if hasattr(ns_window, "setHidesOnDeactivate_"):
                ns_window.setHidesOnDeactivate_(False)             # CRITICAL: don't hide on deactivate
            try:
                if hasattr(ns_window, "_setPreventsActivation_"):
                    ns_window._setPreventsActivation_(True)        # non-activating: never steal focus
            except Exception:
                pass
            if hasattr(ns_window, "setFloatingPanel_"):
                try:
                    ns_window.setFloatingPanel_(True)
                except Exception:
                    pass
            # CRITICAL: pull the window onto the CURRENT space (incl. a fullscreen
            # Space) via the private CGS/SkyLight API. Collection behavior alone does
            # NOT do this — without it the pill stays on Filect's own Space, behind
            # the app the user is in. This is the exact call the quick-search overlay
            # uses to appear over fullscreen apps. We reuse its tested implementation.
            try:
                from .mac_spaces import move_window_to_active_space
                move_window_to_active_space(ns_window.windowNumber())
            except Exception:
                pass
            ns_window.orderFrontRegardless()
        except Exception:
            pass

    # -- animation -------------------------------------------------------------
    def _tick(self) -> None:
        self._phase += 0.16
        # Ease the live level so amplitude jumps don't snap the bars.
        self._level += (self._level_target - self._level) * EASE

        if self._state == STATE_LISTENING:
            center = (N_BARS - 1) / 2.0
            for i in range(N_BARS):
                # Bell weighting: center bars reach higher than the edges.
                dist = abs(i - center) / center
                bell = 1.0 - dist * dist * 0.55
                # Moving sine so neighbouring bars differ and the row ripples.
                wave = 0.5 + 0.5 * math.sin(self._phase + i * 0.7)
                # Gentle idle bob keeps it alive at level 0.
                idle = 0.10 * (0.5 + 0.5 * math.sin(self._phase * 0.6 + i * 0.9))
                target = 0.12 + self._level * bell * wave + idle
                target = max(0.0, min(1.0, target))
                self._heights[i] += (target - self._heights[i]) * EASE
        elif self._state == STATE_DONE:
            self._done_progress = min(1.0, self._done_progress + 0.05)

        self.update()

    # -- painting --------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        pill = QRectF(GLOW_MARGIN, GLOW_MARGIN, PILL_W, PILL_H)

        self._paint_glow(p, pill)

        # Pill body + subtle border.
        p.setPen(Qt.NoPen)
        p.setBrush(PILL_BG)
        p.drawRoundedRect(pill, CORNER, CORNER)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), CORNER, CORNER)

        if self._state == STATE_LISTENING:
            if self._latched:
                self._paint_latch(p, pill)   # pulsing ring under the bars
            self._paint_bars(p, pill)
        elif self._state == STATE_TRANSCRIBING:
            self._paint_dots(p, pill)
        else:
            self._paint_done(p, pill)

        # Search mode: a small magnifying glass so it's distinguishable from dictation
        # without changing the color (keeps the pill consistent with the results popup).
        if self._state != STATE_DONE:
            if self._mode == "search":
                self._paint_search_icon(p, pill)
            elif self._mode == "organize":
                self._paint_organize_icon(p, pill)

        p.end()

    def _paint_search_icon(self, p: QPainter, pill: QRectF) -> None:
        """Small magnifying glass on the left of the pill, marking search mode."""
        r = 7.0
        cx = pill.left() + 30
        cy = pill.center().y()
        pen = QPen(PURPLE_LIGHT, 2.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)
        d = r * 0.72                      # handle from the lens's lower-right
        p.drawLine(QPointF(cx + d, cy + d), QPointF(cx + d + 5, cy + d + 5))

    def _paint_organize_icon(self, p: QPainter, pill: QRectF) -> None:
        """Small folder glyph on the left of the pill, marking organize mode."""
        cx = pill.left() + 26
        cy = pill.center().y()
        pen = QPen(PURPLE_LIGHT, 2.2)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        w, h = 16.0, 11.0
        x = cx - w / 2.0
        y = cy - h / 2.0
        p.drawRoundedRect(QRectF(x, y + 2.0, w, h - 2.0), 1.6, 1.6)   # folder body
        p.drawLine(QPointF(x + 1.5, y + 2.0), QPointF(x + 4.0, y))    # tab
        p.drawLine(QPointF(x + 4.0, y), QPointF(x + 7.5, y))
        p.drawLine(QPointF(x + 7.5, y), QPointF(x + 8.6, y + 2.0))

    def _paint_glow(self, p: QPainter, pill: QRectF) -> None:
        """Fake a soft outer glow with a few expanding low-alpha halos."""
        p.setPen(Qt.NoPen)
        for i in range(GLOW_MARGIN, 0, -6):
            alpha = int(22 * (1 - i / GLOW_MARGIN))
            if alpha <= 0:
                continue
            c = QColor(self._accent())
            c.setAlpha(alpha)
            p.setBrush(c)
            p.drawRoundedRect(pill.adjusted(-i, -i, i, i), CORNER + i, CORNER + i)

    def _paint_latch(self, p: QPainter, pill: QRectF) -> None:
        """Hands-free latch indicator: a persistent pulsing purple ring breathing
        just outside the pill, plus a small ∞ glyph — reads as 'locked on, no
        need to hold'. Drawn in addition to (not instead of) the mic bars."""
        pulse = 0.5 + 0.5 * math.sin(self._phase * 0.9)   # slow breathing, 0..1

        # Breathing ring hugging the pill border (stays within GLOW_MARGIN).
        grow = 3.0 + 4.0 * pulse
        ring = QColor(self._accent_light())
        ring.setAlpha(70 + int(120 * pulse))
        p.setPen(QPen(ring, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(pill.adjusted(-grow, -grow, grow, grow),
                          CORNER + grow, CORNER + grow)

        # ∞ glyph on the right, marking the locked-on hands-free mode.
        glyph = QColor(self._accent_light())
        glyph.setAlpha(180 + int(75 * pulse))
        font = QFont()
        font.setPointSizeF(15.0)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(glyph))
        p.drawText(QRectF(pill.right() - 36, pill.top(), 28, pill.height()),
                   Qt.AlignCenter, "∞")

    def _paint_bars(self, p: QPainter, pill: QRectF) -> None:
        total_w = N_BARS * BAR_W + (N_BARS - 1) * BAR_GAP
        x0 = pill.center().x() - total_w / 2.0
        cy = pill.center().y()

        for i, h01 in enumerate(self._heights):
            h = BAR_MIN_H + h01 * (BAR_MAX_H - BAR_MIN_H)
            x = x0 + i * (BAR_W + BAR_GAP)
            bar = QRectF(x, cy - h / 2.0, BAR_W, h)

            grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            grad.setColorAt(0.0, self._accent_light())
            grad.setColorAt(1.0, self._accent())
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(bar, BAR_W / 2.0, BAR_W / 2.0)

    def _paint_dots(self, p: QPainter, pill: QRectF) -> None:
        """Calm looping 'working' animation: three sequentially pulsing dots."""
        r = 5.0
        gap = 22.0
        cx = pill.center().x()
        cy = pill.center().y()
        p.setPen(Qt.NoPen)
        for i in range(3):
            pulse = 0.5 + 0.5 * math.sin(self._phase * 1.6 - i * 0.9)
            c = QColor(self._accent())
            c.setAlpha(90 + int(150 * pulse))
            p.setBrush(c)
            rad = r * (0.7 + 0.5 * pulse)
            p.drawEllipse(QPointF(cx + (i - 1) * gap, cy), rad, rad)

    def _paint_done(self, p: QPainter, pill: QRectF) -> None:
        """Purple checkmark that draws in, then the whole pill fades out."""
        prog = self._done_progress
        # Fade the window in the back half of the animation.
        if prog > 0.5:
            self.setWindowOpacity(max(0.0, 1.0 - (prog - 0.5) * 2.0))

        cx = pill.center().x()
        cy = pill.center().y()
        # Checkmark defined by three points; reveal it proportional to prog.
        p1 = QPointF(cx - 14, cy)
        p2 = QPointF(cx - 4, cy + 10)
        p3 = QPointF(cx + 16, cy - 12)
        draw = min(1.0, prog * 1.6)

        path = QPainterPath(p1)
        if draw <= 0.5:
            t = draw / 0.5
            path.lineTo(p1 + (p2 - p1) * t)
        else:
            path.lineTo(p2)
            t = (draw - 0.5) / 0.5
            path.lineTo(p2 + (p3 - p2) * t)

        pen = QPen(self._accent_light(), 4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


# --- standalone live demo -----------------------------------------------------
if __name__ == "__main__":
    import os
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    overlay = DictationOverlay()

    # --- headless self-check: every state + the latch look must render without throwing.
    assert overlay._latched is False                 # default is not latched
    overlay.show_state(STATE_LISTENING)
    overlay.set_level(0.6)
    overlay._tick()
    overlay.grab()                                   # forces a paintEvent (push-to-talk look)
    overlay.set_latched(True)
    assert overlay._latched is True
    overlay._tick()
    overlay.grab()                                   # latched look: bars + pulsing ring + ∞
    overlay.set_latched(False)
    assert overlay._latched is False
    overlay.grab()                                   # back to the normal listening look
    for st in (STATE_TRANSCRIBING, STATE_DONE):
        overlay.show_state(st)
        overlay._tick()
        overlay.grab()
    print("dictation_overlay self-check OK")
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        sys.exit(0)

    overlay.show_state(STATE_LISTENING)
    QTimer.singleShot(3000, lambda: overlay.set_latched(True))   # demo: latch after 3s
    QTimer.singleShot(5000, lambda: overlay.set_latched(False))

    # Feed a synthetic amplitude so a human can watch the bars react live.
    t = {"v": 0.0}

    def feed():
        t["v"] += 0.08
        # Two beating sines -> a lively, non-repetitive amplitude envelope.
        level = 0.5 + 0.5 * math.sin(t["v"]) * (0.6 + 0.4 * math.sin(t["v"] * 0.37))
        overlay.set_level(level)

    driver = QTimer()
    driver.setInterval(40)
    driver.timeout.connect(feed)
    driver.start()

    # Cycle through the states so the whole animation set is visible.
    QTimer.singleShot(6000, lambda: overlay.show_state(STATE_TRANSCRIBING))
    QTimer.singleShot(10000, lambda: overlay.show_state(STATE_DONE))
    QTimer.singleShot(11000, lambda: (overlay.show_state(STATE_LISTENING)))

    sys.exit(app.exec())
