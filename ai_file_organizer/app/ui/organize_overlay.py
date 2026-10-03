"""VoiceOS-style floating panel for the voice-driven "organize" feature.

A pure VIEW. No organize logic, no recording — a controller calls these methods
and connects these signals:

    overlay.present()                       # slide down from the top, takes key focus
    overlay.show_listening() / set_level(x) # live mic indicator
    overlay.show_thinking("Analyzing…")     # spinner + message
    overlay.show_plan(summary, folders, target_folder, n_files, n_folders)
    overlay.show_applying()                 # "Organizing…"
    overlay.show_done("…revert in History") # auto-dismisses after ~3s
    overlay.show_error("…")                 # stays until dismissed
    overlay.set_refining(True/False)        # paint the refine mic "recording"
    overlay.dismiss()

Signals: refine_requested, organize_clicked, change_folder_requested, dismissed.

macOS mechanics (non-activating panel over all Spaces / fullscreen) are lifted
verbatim from dictation_overlay.py and are wrapped so the widget still
constructs headless (QT_QPA_PLATFORM=offscreen) and off macOS.

Headless self-check:
    cd /Users/damianosmalliaros/Developer/Mac_app && \
      QT_QPA_PLATFORM=offscreen venv311/bin/python \
      ai_file_organizer/app/ui/organize_overlay.py
"""

import logging
import math
import sys

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    Qt, QTimer, QPointF, QSize, QPropertyAnimation, QEasingCurve, QRectF, Signal,
)
from PySide6.QtGui import (
    QGuiApplication, QCursor, QColor, QPainter, QPen, QLinearGradient, QBrush,
)
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
)

# --- palette (lifted from theme_manager._DARK_COLORS; hardcoded like ----------
# --- dictation_overlay so we carry no app.core.settings dependency) -----------
C = {
    "bg":        "#0A0A12",
    "surface":   "#111119",
    "card":      "#16161F",
    "border":    "#1C1C28",
    "text":      "#E8E8F0",
    "text_2":    "#B0B0C0",
    "text_muted": "#7A7A90",
    "danger_bg":   "rgba(211, 47, 47, 0.14)",
    "danger_border": "rgba(211, 47, 47, 0.45)",
    "danger_text": "#FF6B6B",
}
ACCENT = "#7C4DFF"
ACCENT_LIGHT = "#B39DFF"

CARD_W = 420
SHADOW_MARGIN = 22          # room around the card for the drop shadow
WIDGET_W = CARD_W + SHADOW_MARGIN * 2
TOP_MARGIN = 0              # flush to the top edge (VoiceOS notch style)
STRIP_W = 132              # width of the minimized handle that sits under the notch
TOPBAR_H = 28              # fixed height of the ✦ Organize / ✕ bar (don't query its sizeHint —
                           # it intermittently reads 0 before realize, which cut panels short)

N_BARS = 11
_EASE = 0.28

_WIN_TITLE = "Filect Organize"


# --- small painted children ---------------------------------------------------
class _LevelBars(QWidget):
    """Mic-level bars fed by set_level(); runs its own 60fps timer when active."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level_target = 0.0
        self._level = 0.0
        self._phase = 0.0
        self._heights = [0.0] * N_BARS
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def set_level(self, level: float) -> None:
        self._level_target = max(0.0, min(1.0, float(level)))

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._phase += 0.16
        self._level += (self._level_target - self._level) * _EASE
        center = (N_BARS - 1) / 2.0
        for i in range(N_BARS):
            dist = abs(i - center) / center
            bell = 1.0 - dist * dist * 0.55
            wave = 0.5 + 0.5 * math.sin(self._phase + i * 0.7)
            idle = 0.10 * (0.5 + 0.5 * math.sin(self._phase * 0.6 + i * 0.9))
            target = max(0.0, min(1.0, 0.12 + self._level * bell * wave + idle))
            self._heights[i] += (target - self._heights[i]) * _EASE
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bar_w, gap = 5, 7
        total = N_BARS * bar_w + (N_BARS - 1) * gap
        x0 = (self.width() - total) / 2.0
        cy = self.height() / 2.0
        hi, lo = QColor(ACCENT_LIGHT), QColor(ACCENT)
        for i, h01 in enumerate(self._heights):
            h = 6 + h01 * 38
            x = x0 + i * (bar_w + gap)
            bar = QRectF(x, cy - h / 2.0, bar_w, h)
            grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            grad.setColorAt(0.0, hi)
            grad.setColorAt(1.0, lo)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(bar, bar_w / 2.0, bar_w / 2.0)
        p.end()


class _Spinner(QWidget):
    """Rotating purple arc for thinking/applying states."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self.setFixedSize(30, 30)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 6.0) % 360.0
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(4, 4, self.width() - 8, self.height() - 8)
        pen = QPen(QColor(ACCENT), 3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        # 270-degree arc; Qt angles are in 1/16 deg, counter-clockwise.
        p.drawArc(rect, int(-self._angle * 16), 270 * 16)
        p.end()


class _FitStack(QStackedWidget):
    """A QStackedWidget that sizes to its CURRENT page only. The stock widget reports the
    tallest page's sizeHint forever (the cause of the giant black box / mis-sizing), regardless
    of which page is shown. We forward the current page's size instead — and, crucially, use
    heightForWidth at the real content width for pages with word-wrap labels (error/done/plan),
    whose plain sizeHint height is computed at the wrong width and comes back far too tall."""

    def _current_hint(self):
        w = self.currentWidget()
        if w is None:
            return super().sizeHint()
        s = QSize(w.sizeHint())
        lay = w.layout()
        if lay is not None and lay.hasHeightForWidth():
            # heightForWidth is reliable for word-wrap pages; the plain sizeHint height is
            # computed at the wrong width and comes back far too tall. Use heightForWidth at the
            # fixed card-inner width (self.width() can be stale right after a page rebuild).
            s.setHeight(lay.heightForWidth(CARD_W - 36))
        return s

    def sizeHint(self):
        return self._current_hint()

    def minimumSizeHint(self):
        return self._current_hint()


# --- overlay -------------------------------------------------------------------
class OrganizeOverlay(QWidget):
    """Frameless, non-activating floating panel that previews an organize plan."""

    refine_requested = Signal()         # mic toggle — controller tracks start/stop
    organize_clicked = Signal()         # primary Organize button
    change_folder_requested = Signal()  # "Change" link next to the target folder
    dismissed = Signal()                # user closed it (Esc / ✕)

    _BIG_PLAN = 30                      # plans moving more files than this need a 2nd confirm

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        # Show without activating: this is a non-activating PANEL (see _configure_macos) that
        # takes mouse clicks while another app stays frontmost. On macOS 26 a background app
        # can't become key anyway, so don't let Qt attempt activation on show.
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(_WIN_TITLE)  # lets _configure_macos find the NSWindow
        self.setFixedWidth(WIDGET_W)

        self._state = "idle"       # idle|listening|thinking|plan|applying|done|error
        self._anim = None          # keep a ref so the slide animation isn't GC'd

        self._build_ui()

        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self.dismiss)

    # -- construction ----------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, SHADOW_MARGIN)

        self._card = QFrame()
        self._card.setObjectName("organizeCard")
        # VoiceOS-style: flush to the top edge (square top corners), rounded only at the bottom,
        # near-black — so it reads as expanding DOWN from the notch, not a floating card.
        self._card_style = (
            "QFrame#organizeCard { background-color: #0B0B10; border: none; "
            "border-top-left-radius: 0px; border-top-right-radius: 0px; "
            "border-bottom-left-radius: 22px; border-bottom-right-radius: 22px; }"
        )
        self._card.setStyleSheet(self._card_style)
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(28)          # modest: resized on every hover peek, keep blur cheap
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 170))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        col = QVBoxLayout(self._card)
        col.setContentsMargins(18, 14, 18, 18)
        col.setSpacing(12)

        # Top bar: brand label (left) + ✕ close (right). Doubles as the drag/handle region —
        # clicking it tucks the panel back up into the notch (see mousePressEvent).
        self._topbar = QWidget()
        self._topbar.setStyleSheet("background: transparent;")
        self._topbar.setFixedHeight(TOPBAR_H)       # fixed so _fit's math is deterministic
        self._topbar.setCursor(Qt.PointingHandCursor)
        self._topbar.setToolTip("Click to tuck back into the notch")
        top = QHBoxLayout(self._topbar)
        top.setContentsMargins(0, 0, 0, 0)
        brand = QLabel("✦ Organize")
        brand.setStyleSheet(
            f"color: {ACCENT}; font-size: 13px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        top.addWidget(brand, 0, Qt.AlignVCenter)
        top.addStretch(1)
        close = QPushButton("✕")
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedSize(24, 24)
        close.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {C['text_muted']}; font-size: 14px; border-radius: 12px; }}"
            f"QPushButton:hover {{ background: {C['border']}; color: {C['text']}; }}"
        )
        close.clicked.connect(self._on_dismiss)
        top.addWidget(close, 0, Qt.AlignVCenter)
        col.addWidget(self._topbar)

        # Swappable content. _FitStack sizes to the current page only (see its docstring).
        self._stack = _FitStack()
        self._stack.setStyleSheet("background: transparent;")
        col.addWidget(self._stack)


        self._page_listening = self._make_listening_page()   # 0
        self._page_thinking = self._make_thinking_page()      # 1
        self._page_plan = QWidget()                           # 2 (rebuilt per call)
        self._page_plan.setStyleSheet("background: transparent;")
        QVBoxLayout(self._page_plan).setContentsMargins(0, 0, 0, 0)
        self._page_applying = self._make_applying_page()      # 3
        self._page_done = self._make_message_page("done")     # 4
        self._page_error = self._make_message_page("error")   # 5
        for pg in (self._page_listening, self._page_thinking, self._page_plan,
                   self._page_applying, self._page_done, self._page_error):
            self._stack.addWidget(pg)

        # Minimized handle: a small grabber shown at the notch when the panel is tucked away.
        # Clicking anywhere on it pops the panel back down (see minimize()/restore()).
        self._strip = QWidget()
        self._strip.setStyleSheet("background: transparent;")
        _sl = QHBoxLayout(self._strip)
        _sl.setContentsMargins(0, 3, 0, 5)
        _sl.addStretch(1)
        _grab = QFrame()
        _grab.setFixedSize(46, 5)
        _grab.setStyleSheet("background: rgba(255,255,255,0.45); border-radius: 2px; border: none;")
        _sl.addWidget(_grab)
        _sl.addStretch(1)
        self._strip.setVisible(False)
        col.addWidget(self._strip)

        self._minimized = False
        self._state = "thinking"
        self._plan_stack_h = 225        # deterministic plan height; recomputed per plan

    def _make_listening_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(10)
        self._bars = _LevelBars()
        lay.addWidget(self._bars)
        lbl = QLabel("Listening… tap Stop when you're done")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {C['text_2']}; font-size: 13px; background: transparent; border: none;"
        )
        lay.addWidget(lbl)
        # A visible Stop control. Tapping the mic swaps THIS page in for the plan page, so the
        # mic button that started recording is no longer on screen — without this button there
        # is no way to stop except the Fn+⌥ hotkey. It reuses refine_requested, which the
        # controller toggles: while recording, that signal stops + transcribes.
        self._stop_btn = QPushButton("⏹  Stop")
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setFixedHeight(40)
        self._style_organize(self._stop_btn, False)
        self._stop_btn.clicked.connect(self.refine_requested.emit)
        lay.addWidget(self._stop_btn)
        return pg

    def _make_thinking_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)   # compact: no "analyzing" box bigger than its text
        lay.setSpacing(10)
        lay.addStretch(1)
        self._thinking_spinner = _Spinner()
        lay.addWidget(self._thinking_spinner, 0, Qt.AlignVCenter)
        self._thinking_label = QLabel("Analyzing your files…")
        self._thinking_label.setStyleSheet(
            f"color: {C['text']}; font-size: 14px; background: transparent; border: none;"
        )
        lay.addWidget(self._thinking_label, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        return pg

    def _make_applying_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(pg)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)
        lay.addStretch(1)
        self._applying_spinner = _Spinner()
        lay.addWidget(self._applying_spinner, 0, Qt.AlignVCenter)
        lbl = QLabel("Organizing…")
        lbl.setStyleSheet(
            f"color: {C['text']}; font-size: 14px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        lay.addWidget(lbl, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        return pg

    def _make_message_page(self, kind: str) -> QWidget:
        """Shared layout for the done / error pages (glyph + label, plus a folder picker on
        the error page when the AI couldn't resolve the folder)."""
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(pg)
        outer.setContentsMargins(0, 8, 0, 8)
        outer.setSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(10)
        glyph = QLabel("✓" if kind == "done" else "⚠")
        color = ACCENT_LIGHT if kind == "done" else C["danger_text"]
        glyph.setStyleSheet(
            f"color: {color}; font-size: 18px; font-weight: 700; "
            "background: transparent; border: none;"
        )
        row.addWidget(glyph, 0, Qt.AlignTop)
        label = QLabel("")
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {C['text']}; font-size: 13px; background: transparent; border: none;"
        )
        row.addWidget(label, 1)
        outer.addLayout(row)
        if kind == "done":
            self._done_label = label
        else:
            self._error_label = label
            # Manual folder picker — shown only when the AI couldn't tell which folder to use.
            self._error_pick_btn = QPushButton("📁  Choose folder…")
            self._error_pick_btn.setCursor(Qt.PointingHandCursor)
            self._error_pick_btn.setFixedHeight(38)
            self._style_organize(self._error_pick_btn, False)
            self._error_pick_btn.clicked.connect(self.change_folder_requested.emit)
            self._error_pick_btn.setVisible(False)
            outer.addWidget(self._error_pick_btn)
        return pg

    def resizeEvent(self, e) -> None:
        # Any resize (ours per-state, or a Qt-internal re-sync) can make Qt rewrite the native
        # styleMask and drop the non-activating-panel bit — the funk beep / dead clicks. Restore
        # it after every resize. Idempotent, so it only acts when the bit was actually lost.
        super().resizeEvent(e)
        self._reassert_panel_style()

    def minimize(self) -> None:
        """Tuck the panel into a small clickable handle at the notch (reopenable by clicking it),
        without fully closing. Unlike the ✕/Cancel close, the content is kept so a click on the
        handle pops it straight back down."""
        if self._minimized:
            return
        self._minimized = True
        self._done_timer.stop()
        self._stop_anim()
        self._topbar.setVisible(False)
        self._stack.setVisible(False)
        self._strip.setVisible(True)
        self._card.setStyleSheet("QFrame#organizeCard { background: transparent; border: none; }")
        eff = self._card.graphicsEffect()
        if eff is not None:
            eff.setEnabled(False)          # no shadow on the bare handle
        self.setFixedWidth(STRIP_W)
        geo = self._screen_geo()
        if geo is not None:
            self.setMinimumHeight(0)
            self.resize(STRIP_W, 22)
            self.move(geo.x() + (geo.width() - STRIP_W) // 2, geo.y() + TOP_MARGIN)
        self._reassert_panel_style()

    def restore(self) -> None:
        """Pop the panel back down from the notch handle to its full content."""
        self._minimized = False
        self._strip.setVisible(False)
        self._topbar.setVisible(True)
        self._stack.setVisible(True)
        self._card.setStyleSheet(self._card_style)
        eff = self._card.graphicsEffect()
        if eff is not None:
            eff.setEnabled(True)
        self.setFixedWidth(WIDGET_W)
        self._fit()
        self._reanchor()
        self._reassert_panel_style()

    def mousePressEvent(self, e) -> None:
        # Minimized handle at the notch -> a click pops the panel back down.
        if self._minimized:
            self.restore()
            e.accept()
            return
        # Message states (finding/analyzing/error/done) have no controls to keep, so a click
        # anywhere fully closes them (tucks up into the notch and gone).
        if self._state in ("thinking", "error", "done"):
            self._on_dismiss()
            e.accept()
            return
        # Plan / listening: a click on the top handle (the ✦ Organize bar, flush under the notch)
        # MINIMIZES to the notch handle — kept + reopenable. Buttons/content handle their own
        # clicks and never reach here.
        try:
            handle_bottom = self._topbar.mapTo(self, self._topbar.rect().bottomLeft()).y()
        except Exception:
            handle_bottom = 44
        if e.position().y() <= handle_bottom + 4:
            self.minimize()
            e.accept()
            return
        super().mousePressEvent(e)

    # -- public: lifecycle -----------------------------------------------------
    def present(self) -> None:
        """Slide the panel down from under the notch (fully expanded). The top handle MINIMIZES
        it to a small handle at the notch (click that to pop it back down); the ✕/Cancel fully
        close it. See _configure_macos for the accessory-app non-activating-panel (clickable) fix."""
        self._done_timer.stop()
        # Always open expanded — clear any minimized state from a previous run.
        if self._minimized:
            self._minimized = False
            self._strip.setVisible(False)
            self._topbar.setVisible(True)
            self._stack.setVisible(True)
            self._card.setStyleSheet(self._card_style)
            eff = self._card.graphicsEffect()
            if eff is not None:
                eff.setEnabled(True)
            self.setFixedWidth(WIDGET_W)
        # Default the initial size to the thinking page, not whatever page was last current
        # (the listening page is tall — measuring it here made the panel flash oversized).
        self._stack.setCurrentWidget(self._page_thinking)
        geo = self._screen_geo()
        self._fit()
        self.setWindowOpacity(1.0)
        if geo is None:
            self.show()
            self._configure_macos_deferred()
            return
        x, end_y = self._anchor_pos(geo)
        start_y = geo.y() - self.height()          # fully above the top edge
        self.move(x, start_y)
        if not self.isVisible():
            self.show()
        self._configure_macos_deferred()
        self._slide_to(x, end_y)

    def dismiss(self) -> None:
        """Slide up and hide."""
        self._done_timer.stop()
        self._stop_anim()
        geo = self._screen_geo()
        if geo is None or not self.isVisible():
            self.hide()
            return
        self._slide_to(self.x(), geo.y() - self.height(), on_done=self.hide)

    def set_level(self, level: float) -> None:
        """0..1 mic level; drives the listening indicator."""
        self._bars.set_level(level)

    def set_refining(self, is_recording: bool) -> None:
        """Paint the plan-page refine mic as 'recording' (red) or idle."""
        btn = getattr(self, "_refine_btn", None)
        if btn is not None:
            self._style_refine(btn, is_recording)

    # -- public: state pages ---------------------------------------------------
    def show_listening(self) -> None:
        self._state = "listening"
        self._stop_anim()
        self._bars.start()
        self._show_page(self._page_listening)

    def show_thinking(self, msg: str = "Analyzing your files…") -> None:
        self._state = "thinking"
        self._stop_anim()
        self._thinking_label.setText(msg)
        self._thinking_spinner.start()
        self._show_page(self._page_thinking)

    def show_plan(self, summary: str, folders: dict, target_folder: str,
                  file_count: int, folder_count: int) -> None:
        self._state = "plan"
        self._stop_anim()
        self._build_plan(summary, folders or {}, target_folder,
                         file_count, folder_count)
        self._show_page(self._page_plan)

    def show_plan_page(self) -> None:
        """Re-show the already-built plan without rebuilding it — e.g. after a refine
        recording that captured nothing, so the panel doesn't get stuck on 'Listening…'."""
        if getattr(self, "_page_plan", None) is None:
            return
        self._state = "plan"
        self._stop_anim()
        self._show_page(self._page_plan)

    def show_applying(self) -> None:
        self._state = "applying"
        self._stop_anim()
        self._applying_spinner.start()
        self._show_page(self._page_applying)

    def show_done(self, revert_hint: str) -> None:
        self._state = "done"
        self._stop_anim()
        self._done_label.setText(revert_hint or "Organized.")
        self._show_page(self._page_done)
        self._done_timer.start(3000)   # auto-dismiss

    def show_error(self, msg: str, allow_pick_folder: bool = False) -> None:
        self._state = "error"
        self._stop_anim()
        self._done_timer.stop()
        self._error_label.setText(msg or "Something went wrong.")
        if hasattr(self, "_error_pick_btn"):
            self._error_pick_btn.setVisible(bool(allow_pick_folder))
        self._show_page(self._page_error)

    # -- plan rendering --------------------------------------------------------
    def _clear_layout(self, layout):
        """Recursively remove everything from a layout — widgets AND nested layouts. A
        shallow clear left old footer layouts behind, which stacked up duplicate
        Cancel/Organize rows on every re-render."""
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)
                    child.deleteLater()

    def _build_plan(self, summary, folders, target_folder, file_count, folder_count):
        # Clear the previous plan content (recursively — see _clear_layout).
        lay = self._page_plan.layout()
        self._clear_layout(lay)
        lay.setSpacing(12)

        head = QLabel(summary or f"Move {file_count} files into {folder_count} folders")
        head.setWordWrap(True)
        head.setStyleSheet(
            f"color: {C['text']}; font-size: 16px; font-weight: 700; "
            "background: transparent; border: none;"
        )
        lay.addWidget(head)

        # Target-folder row: "Into:  <folder>   Change".
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background-color: {C['surface']}; border: 1px solid {C['border']}; "
            "border-radius: 10px; } QLabel { background: transparent; border: none; }"
        )
        rl = QHBoxLayout(row)
        rl.setContentsMargins(12, 8, 12, 8)
        rl.setSpacing(8)
        into = QLabel("\U0001F4C1")
        into.setStyleSheet("background: transparent; border: none; font-size: 13px;")
        rl.addWidget(into, 0, Qt.AlignVCenter)
        tgt = QLabel(target_folder or "—")
        tgt.setStyleSheet(
            f"color: {C['text_2']}; font-size: 12px; background: transparent; border: none;"
        )
        tgt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(tgt, 1, Qt.AlignVCenter)
        change = QPushButton("Change")
        change.setCursor(Qt.PointingHandCursor)
        change.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {ACCENT}; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {ACCENT_LIGHT}; }}"
        )
        change.clicked.connect(self.change_folder_requested.emit)
        rl.addWidget(change, 0, Qt.AlignVCenter)
        lay.addWidget(row)

        # Expandable folder -> files list inside a capped scroll area.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(300)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        for name, files in folders.items():
            hl.addWidget(self._make_folder_group(name, list(files or [])))
        hl.addStretch(1)
        scroll.setWidget(host)
        # A QScrollArea's sizeHint ignores its content, so give it a concrete height derived from
        # the folder COUNT (reliable — no measuring a not-yet-realized layout), capped at 300 so
        # long plans scroll. ~52px per collapsed folder row + the inter-row spacing.
        n = max(1, len(folders))
        scroll.setFixedHeight(min(n * 52 + (n - 1) * 8, 300))
        lay.addWidget(scroll)

        # Footer: refine-by-voice mic + label, then the primary Organize button.
        foot = QVBoxLayout()
        foot.setSpacing(10)

        refine_row = QHBoxLayout()
        refine_row.setSpacing(8)
        self._refine_btn = QPushButton()
        self._refine_btn.setCursor(Qt.PointingHandCursor)
        self._refine_btn.setFixedSize(34, 34)
        self._style_refine(self._refine_btn, False)
        self._refine_btn.clicked.connect(self.refine_requested.emit)
        refine_row.addWidget(self._refine_btn, 0, Qt.AlignVCenter)
        refine_lbl = QLabel("Change by voice — tap 🎙 or press Fn+⌥")
        refine_lbl.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 12px; background: transparent; border: none;"
        )
        refine_row.addWidget(refine_lbl, 0, Qt.AlignVCenter)
        refine_row.addStretch(1)
        foot.addLayout(refine_row)

        # Big-plan guardrail: warn + require a second confirm click above the threshold.
        self._plan_file_count = int(file_count or 0)
        self._organize_armed = False
        if self._plan_file_count > self._BIG_PLAN:
            warn = QLabel(f"⚠  This will move {self._plan_file_count} files — review before confirming.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #E0A23C; font-size: 12px; font-weight: 600; "
                               "background: transparent; border: none;")
            foot.addWidget(warn)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(40)
        cancel.setStyleSheet(
            f"QPushButton {{ background-color: {C['surface']}; color: {C['text_2']}; "
            f"border: 1px solid {C['border']}; border-radius: 10px; font-size: 13px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {C['text']}; border-color: {C['text_muted']}; }}"
        )
        cancel.clicked.connect(self._on_dismiss)
        btn_row.addWidget(cancel, 0)

        n = self._plan_file_count
        self._organize_btn = QPushButton(f"✓  Organize {n} file{'s' if n != 1 else ''}")
        self._organize_btn.setCursor(Qt.PointingHandCursor)
        self._organize_btn.setFixedHeight(40)
        self._style_organize(self._organize_btn, False)
        self._organize_btn.clicked.connect(self._on_organize_pressed)
        btn_row.addWidget(self._organize_btn, 1)
        foot.addLayout(btn_row)
        lay.addLayout(foot)

        # Deterministic plan height for _content_height()/_fit — computed from the folder COUNT,
        # not by measuring a not-yet-realized layout (which raced and clipped the buttons). The
        # only variable piece is the header (can wrap): head + row(34) + scroll + footer(84) +
        # 3 inter-item gaps(36) + the big-plan warning row. Matches the measured 225/345/473.
        head.setFixedWidth(CARD_W - 36)
        head_h = max(head.heightForWidth(CARD_W - 36), head.sizeHint().height(), 19)
        head.setMinimumWidth(0)
        head.setMaximumWidth(16777215)
        nfolders = max(1, len(folders))
        scroll_h = min(nfolders * 52 + (nfolders - 1) * 8, 300)
        warn_extra = 44 if self._plan_file_count > self._BIG_PLAN else 0
        # Generous FLOOR estimate (footer budget 124 > the 84 measured offscreen — real fonts
        # render taller). This never clips in the first frame; _refit_plan then corrects it to the
        # EXACT realized height on the next tick.
        self._plan_stack_h = head_h + 34 + scroll_h + 124 + 36 + warn_extra

    def _style_organize(self, btn, danger):
        bg = "#C0392B" if danger else ACCENT
        hover = "#D84A3B" if danger else ACCENT_LIGHT
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: white; border: none; "
            "border-radius: 10px; font-size: 14px; font-weight: 700; }"
            f"QPushButton:hover {{ background-color: {hover}; }}"
        )

    def _on_organize_pressed(self):
        # Big plans need a second, explicit confirm so a 100+-file move can't happen on one tap.
        if self._plan_file_count > self._BIG_PLAN and not self._organize_armed:
            self._organize_armed = True
            self._organize_btn.setText(f"Move {self._plan_file_count} files?  Click to confirm")
            self._style_organize(self._organize_btn, True)
            return
        self.organize_clicked.emit()

    def _make_folder_group(self, name: str, files: list) -> QWidget:
        """A folder header that toggles its file list open/closed."""
        group = QFrame()
        group.setStyleSheet(
            f"QFrame {{ background-color: {C['surface']}; border: 1px solid {C['border']}; "
            "border-radius: 10px; }"
        )
        gl = QVBoxLayout(group)
        gl.setContentsMargins(4, 4, 4, 4)
        gl.setSpacing(0)

        files_host = QWidget()
        files_host.setStyleSheet("background: transparent;")
        fl = QVBoxLayout(files_host)
        fl.setContentsMargins(14, 4, 10, 8)
        fl.setSpacing(4)
        for fname in files:
            f = QLabel(fname)
            f.setStyleSheet(
                f"color: {C['text_2']}; font-size: 12px; background: transparent; border: none;"
            )
            fl.addWidget(f)
        files_host.setVisible(False)   # start collapsed

        header = QPushButton(f"▸  {name}   ({len(files)})")
        header.setCheckable(True)
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet(
            "QPushButton { text-align: left; background: transparent; border: none; "
            f"color: {C['text']}; font-size: 13px; font-weight: 600; padding: 8px 10px; }}"
            f"QPushButton:hover {{ color: {ACCENT_LIGHT}; }}"
        )

        def _toggle(checked, host=files_host, btn=header, n=name, cnt=len(files)):
            host.setVisible(checked)
            arrow = "▾" if checked else "▸"
            btn.setText(f"{arrow}  {n}   ({cnt})")
            self._fit()

        header.toggled.connect(_toggle)
        gl.addWidget(header)
        gl.addWidget(files_host)
        return group

    def _mic_icon(self, color_hex: str):
        """Draw a crisp monochrome mic in the given color — an emoji can't be CSS-tinted, so on
        the dark card it rendered as an invisible dark blob."""
        from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor
        from PySide6.QtCore import Qt, QRectF, QPointF
        S = 22                       # logical icon size — painter works in these units
        pm = QPixmap(S * 2, S * 2)   # 2x backing for retina
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor(color_hex)
        pen = QPen(col, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx = S / 2.0                 # 11 — CENTER (the old code used 22 = the edge, so it clipped)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(cx - 3.0, 3.0, 6.0, 9.0), 3.0, 3.0)      # mic head (filled capsule)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(cx - 5.5, 5.0, 11.0, 11.0), 200 * 16, 140 * 16)  # cradle
        p.drawLine(QPointF(cx, 16.0), QPointF(cx, 19.0))                  # stem
        p.drawLine(QPointF(cx - 3.5, 19.0), QPointF(cx + 3.5, 19.0))      # base
        p.end()
        return QIcon(pm)

    def _style_refine(self, btn: QPushButton, recording: bool) -> None:
        from PySide6.QtCore import QSize
        if recording:
            btn.setIcon(self._mic_icon("#FFFFFF"))
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {C['danger_text']}; "
                "border: none; border-radius: 17px; }"
            )
        else:
            btn.setIcon(self._mic_icon(ACCENT_LIGHT))
            btn.setStyleSheet(
                "QPushButton { background-color: rgba(124, 77, 255, 0.22); "
                f"border: 1.5px solid {ACCENT}; border-radius: 17px; }}"
                f"QPushButton:hover {{ background-color: rgba(124, 77, 255, 0.38); "
                f"border-color: {ACCENT_LIGHT}; }}"
            )
        btn.setIconSize(QSize(22, 22))

    # -- paging / sizing / anchoring ------------------------------------------
    def _show_page(self, page: QWidget) -> None:
        self._stack.setCurrentWidget(page)
        self._fit()
        self._reanchor()

    # Fixed content heights for the states that are the SAME every time (measured once, realized).
    # Only the plan is dynamic — see _build_plan's self._plan_stack_h.
    _STACK_H = {"thinking": 104, "applying": 104, "listening": 136, "done": 70, "error": 60}

    def _content_height(self) -> int:
        """Height of the current page's content, chosen deterministically (no runtime measuring,
        which raced and clipped). Constant states use fixed values; the plan uses the value
        computed from its folder count in _build_plan."""
        st = self._state
        if st == "plan":
            return int(getattr(self, "_plan_stack_h", 225))
        if st == "error":
            # Fixed heights, generous enough for the real app's (larger-than-offscreen) fonts to
            # wrap to 2-3 lines without clipping. Two set values: with vs without the folder picker.
            btn = getattr(self, "_error_pick_btn", None)
            return 134 if (btn is not None and btn.isVisible()) else 76
        return self._STACK_H.get(st, 34)

    def _fit(self) -> None:
        """Resize the window to the current page. The height is DETERMINISTIC (_content_height):
        constant states are fixed, the plan grows with its folder count. We don't measure the
        live layout — that raced (plan read at ~30px → clipped) and the card's drop-shadow effect
        made its sizeHint report a stale height."""
        col = self._card.layout()
        cm = col.contentsMargins()
        om = self.layout().contentsMargins()
        topbar_h = 0 if self._minimized else TOPBAR_H
        target = (om.top() + cm.top() + topbar_h + (col.spacing() if topbar_h else 0)
                  + self._content_height() + cm.bottom() + om.bottom())
        self.setMinimumHeight(0)
        self.resize(self.width(), max(60, int(target)))
        self._reassert_panel_style()   # resizing can drop the non-activating style -> restore it

    def _reanchor(self) -> None:
        """Re-center at the top without re-running the slide (content changed)."""
        if not self.isVisible():
            return
        geo = self._screen_geo()
        if geo is None:
            return
        x, y = self._anchor_pos(geo)
        self.move(x, y)

    def _anchor_pos(self, geo):
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + TOP_MARGIN                    # window top flush with the screen top
        return x, y

    def _screen_geo(self):
        # VoiceOS-style: always drop under the NOTCH on the built-in display, wherever the
        # cursor is. Fall back to the cursor's screen (then primary) when there's no notch.
        geo = self._notch_screen_geo()
        if geo is not None:
            return geo
        screen = None
        try:
            screen = QGuiApplication.screenAt(QCursor.pos())
        except Exception:
            screen = None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.geometry()   # full geometry so we sit flush at the true top (the notch)

    def _notch_screen_geo(self):
        """Qt geometry of the built-in display that has the notch (safeAreaInsets.top > 0),
        or None when there isn't one. Matched to its QScreen by logical size so the slide
        animation keeps using Qt coordinates."""
        if sys.platform != "darwin":
            return None
        try:
            from AppKit import NSScreen
        except Exception:
            return None
        try:
            notch = None
            for s in NSScreen.screens():
                try:
                    if s.safeAreaInsets().top > 0:   # only the notch display has a top inset
                        notch = s
                        break
                except Exception:
                    continue
            if notch is None:
                return None
            fr = notch.frame()
            nw, nh = round(fr.size.width), round(fr.size.height)
            for qs in QGuiApplication.screens():
                g = qs.geometry()
                if abs(g.width() - nw) <= 2 and abs(g.height() - nh) <= 2:
                    return g
        except Exception:
            return None
        return None

    def _slide_to(self, x: int, y: int, on_done=None) -> None:
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setEndValue(QPointF(x, y).toPoint())
        if on_done is not None:
            anim.finished.connect(on_done)
        self._anim = anim
        anim.start()

    def _stop_anim(self) -> None:
        self._bars.stop()
        self._thinking_spinner.stop()
        self._applying_spinner.stop()

    def _reassert_panel_style(self) -> None:
        """Re-apply the non-activating-panel style bit. Qt re-syncs the native window's
        styleMask when geometry constraints change (our per-state resize in _fit), which drops
        NSWindowStyleMaskNonactivatingPanel — and brings back the funk beep / dead clicks. Only
        sets it when it was actually lost (idempotent → no style churn, no shadow-blur crash)."""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
        except Exception:
            return
        try:
            ns = None
            for w in NSApp.windows():
                try:
                    if w.title() == _WIN_TITLE:
                        ns = w
                        break
                except Exception:
                    continue
            if ns is None:
                return
            _NONACT = 1 << 7
            if hasattr(ns, "styleMask") and hasattr(ns, "setStyleMask_"):
                cur = ns.styleMask()
                if not (cur & _NONACT):
                    ns.setStyleMask_(cur | _NONACT)
                    if hasattr(ns, "setBecomesKeyOnlyIfNeeded_"):
                        ns.setBecomesKeyOnlyIfNeeded_(True)
                    logger.info(f"[organize overlay] re-asserted non-activating style "
                                f"(was dropped on resize) -> {ns.styleMask()}")
            try:
                ns.setLevel_(1000)
            except Exception:
                pass
        except Exception:
            pass

    # -- macOS non-activating / all-Spaces config (verbatim pattern) ----------
    def _configure_macos_deferred(self) -> None:
        if sys.platform != "darwin":
            return
        self._configure_macos()
        QTimer.singleShot(10, self._configure_macos)
        QTimer.singleShot(60, self._configure_macos)

    def _configure_macos(self) -> None:
        """Float over all Spaces / fullscreen apps without stealing focus."""
        try:
            from AppKit import NSApp
        except Exception:
            return
        try:
            ns_window = None
            for w in NSApp.windows():
                try:
                    if w.title() == _WIN_TITLE:
                        ns_window = w
                        break
                except Exception:
                    continue
            if ns_window is None:
                return
            ns_window.setCollectionBehavior_((1 << 0) | (1 << 8))  # AllSpaces | FullScreenAux
            ns_window.setLevel_(1000)                               # NSScreenSaverWindowLevel
            if hasattr(ns_window, "setHidesOnDeactivate_"):
                ns_window.setHidesOnDeactivate_(False)
            # ROOT CAUSE (verified on macOS 26 / Tahoe): a background accessory app summoned by
            # a global hotkey CANNOT become the key window — isKeyWindow stays False even with
            # cooperative NSApp.activate() AND a Regular-policy flip, because Tahoe refuses to
            # activate an app the user didn't click into. Every "make it key" attempt therefore
            # produced the funk beep + dead buttons. The fix is to NOT need key focus: make this
            # a NON-ACTIVATING PANEL (NSWindowStyleMaskNonactivatingPanel). Such a panel receives
            # mouse clicks while another app stays frontmost — no activation, no Dock icon, no
            # beep. The panel is entirely mouse-driven (buttons + mic), so it never needs the
            # keyboard, which is the only thing a non-key window can't get.
            _NONACTIVATING_PANEL = 1 << 7   # NSWindowStyleMaskNonactivatingPanel (128)
            try:
                # Set the style mask ONCE. _configure_macos runs several times per present
                # (0/10/60ms) and re-setting the mask each time forces extra window-frame
                # recomputes + repaints — under rapid show/hide that raced the panel's
                # drop-shadow blur into a crash. Idempotent set avoids that churn.
                if hasattr(ns_window, "setStyleMask_") and hasattr(ns_window, "styleMask"):
                    _cur = ns_window.styleMask()
                    if not (_cur & _NONACTIVATING_PANEL):
                        ns_window.setStyleMask_(_cur | _NONACTIVATING_PANEL)
                if hasattr(ns_window, "setFloatingPanel_"):
                    ns_window.setFloatingPanel_(True)
                # A non-activating panel takes clicks without becoming key; leave key status to
                # AppKit (becomesKeyOnlyIfNeeded) instead of forcing it.
                if hasattr(ns_window, "setBecomesKeyOnlyIfNeeded_"):
                    ns_window.setBecomesKeyOnlyIfNeeded_(True)
                try:
                    from .mac_spaces import move_window_to_active_space
                    move_window_to_active_space(ns_window.windowNumber())
                except Exception:
                    pass
                ns_window.orderFrontRegardless()
                logger.info(f"[organize overlay] non-activating panel, style={ns_window.styleMask()}")
            except Exception as e:
                logger.warning(f"[organize overlay] panel config failed: {e}")
                try:
                    ns_window.orderFrontRegardless()
                except Exception:
                    pass
        except Exception:
            pass

    # -- dismiss on Esc / ✕ ----------------------------------------------------
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self._on_dismiss()
            return
        super().keyPressEvent(event)

    def _on_dismiss(self) -> None:
        self.dismissed.emit()
        self.dismiss()


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    ov = OrganizeOverlay()

    # Signals exist and are the right type.
    for sig in ("refine_requested", "organize_clicked",
                "change_folder_requested", "dismissed"):
        assert hasattr(ov, sig), f"missing signal {sig}"
        getattr(ov, sig).connect(lambda *a: None)

    ov.present()
    assert ov._state in ("idle", "listening", "plan", "thinking")  # present keeps page

    ov.show_listening()
    assert ov._state == "listening"
    ov.set_level(0.5)
    ov.set_level(1.5)   # clamp path
    ov.set_level(-1.0)

    ov.show_thinking()
    assert ov._state == "thinking"
    ov.show_thinking("Reading 12 files…")

    ov.show_plan(
        "Move 3 files into 2 folders",
        {"Invoices": ["a.pdf", "b.pdf"], "Screenshots": ["c.png"]},
        "~/Documents/Sorted", 3, 2,
    )
    assert ov._state == "plan"
    assert ov._refine_btn is not None
    ov.set_refining(True)
    ov.set_refining(False)

    # Toggle a folder group open/closed (exercises the expand path + _fit).
    for child in ov._page_plan.findChildren(QPushButton):
        if child.isCheckable():
            child.setChecked(True)
            child.setChecked(False)

    ov.show_applying()
    assert ov._state == "applying"

    ov.show_done("Organized — revert anytime in History.")
    assert ov._state == "done"
    assert ov._done_timer.isActive()

    ov.show_error("Couldn't reach the organize service.")
    assert ov._state == "error"

    # Plan with empty/missing folders must not throw.
    ov.show_plan("Nothing to move", {}, "", 0, 0)

    ov.dismiss()

    print("organize_overlay self-check OK")
