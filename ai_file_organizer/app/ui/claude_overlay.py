"""Ambient "Assistant" notch overlay.

A calm, VoiceOS-style surface for the Claude agent. It RESTS as a small, state-coloured handle
tucked under the notch and only reveals its content when you HOVER it — it never drops down on
its own. The handle itself carries the signal:

    idle      dim grey        (no task)
    working   purple, breathing
    needs_you AMBER, breathing + a dot   ← an Approve/Deny is pending (it blocks the task)
    done      green           (fades back after a few seconds)
    error     red

Hovering expands the panel to show the current step, or — the point of the whole thing — a
compact Approve/Deny card so you can clear a blocked tool call without leaving the app you're in.
The full in-tab Activity feed and approval card stay as the detailed view; this is the ambient,
glance-and-approve layer. Mouse-only + non-activating, so it never steals focus.

Driven by the AssistantPage's existing worker signals; see wiring in assistant_page.py.

macOS panel mechanics are shared with the organize overlay via notch_panel.py.

Headless self-check:
    cd /Users/damianosmalliaros/Developer/Mac_app && \
      QT_QPA_PLATFORM=offscreen venv311/bin/python \
      ai_file_organizer/app/ui/claude_overlay.py
"""
import logging
import math
import sys

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    Qt, QTimer, QPointF, QRectF, QSize, QPropertyAnimation, QEasingCurve, Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen, QGuiApplication, QCursor
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QSizePolicy, QGraphicsDropShadowEffect, QTextEdit, QSlider,
)

try:
    from . import notch_panel
except ImportError:                      # running this file directly (self-check)
    import notch_panel

# --- palette (VoiceOS-style: soft near-black card, muted greys, Claude-orange accent) ----
C = {
    "text":       "#F2F2F4",
    "text_2":     "#C2C2CA",
    "text_muted": "#8A8A92",
    "surface":    "#202024",   # pill / control background
    "border":     "#2E2E34",
}
CARD_BG = "#0A0A0C"            # near-black so the card's top MERGES with the physical notch
ACCENT = "#D97757"            # Claude orange — brand icon + the "working" accent
ACCENT_LIGHT = "#E8A98C"
AMBER = "#E0A23C"           # "your turn" — an approval is waiting
GREEN = "#3FB860"
RED = "#F2645A"

CARD_W = 440
SHADOW_MARGIN = 22
WIDGET_W = CARD_W + SHADOW_MARGIN * 2
TOP_MARGIN = 0
STRIP_W = 150               # width of the resting handle
TOPBAR_H = 24
FOOTER_H = 22               # keycap shortcut hint row
MIN_H = 16                  # pill strip height BELOW the notch band (window = notch band + this)
WIN_TITLE = "Filect Assistant"   # unique — notch_panel finds the NSWindow by this

# State -> handle colour. working/needs_you also breathe (animated alpha).
_STATE_COLOR = {
    "idle":      QColor(255, 255, 255, 90),
    "working":   QColor(ACCENT),
    "needs_you": QColor(AMBER),
    "done":      QColor(GREEN),
    "error":     QColor(RED),
}
# Deterministic content heights per state (no runtime layout measuring — that raced in organize).
_STACK_H = {"working": 22, "needs_you": 116, "done": 118, "error": 56}
DETAILS_H = 166      # height of the live "Details" feed when expanded
TRANSCRIPT_H = 56    # the "what you said" bubble at the top of the card
CTRL_H = 32          # persistent control row (model pill · Details · primary action)
MODELPANEL_H = 150   # the inline "Model & effort" panel (model dropdown closed)
MODEL_LIST_H = 96    # extra height when the dropdown list is open (3 rows — the selected is hidden)
_MODELS = ("opus", "sonnet", "haiku", "fable")
# Display names carry the version (the alias still resolves to Claude Code's latest of each family).
_MODEL_NAMES = {"opus": "Opus 5.5", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "fable": "Fable 5.1"}
_EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]       # slider index 0..4 ("" = Auto, off-slider)
_EFFORT_NAMES = {"": "Auto", "low": "Low", "medium": "Medium",
                 "high": "High", "xhigh": "Extra high", "max": "Max"}

FLASH_MS = 3500      # how long the pop-up stays open; each new step re-arms it (follows activity)


def _load_logo(px: int):
    """Use the official Claude logo IF one is present at app/ui/assets/claude-logo.(png|svg|jpg).
    We don't redraw Anthropic's mark in code — just load the real file when it's there; otherwise
    the header shows a neutral placeholder glyph."""
    import os
    from PySide6.QtGui import QPixmap
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    for name in ("claude-logo.png", "claude-logo.svg", "claude-logo.jpg", "claude.png"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            pm = QPixmap(p)
            if not pm.isNull():
                return pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return None


class _Handle(QWidget):
    """The notch hotspot. INVISIBLE at rest — it draws nothing unless you hover it (a faint pill
    appears so you know where to click) or the task is blocked waiting on you (needs_you: a quiet
    amber pill + dot that breathes, the one thing allowed to show unprompted). Click opens."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"
        self._hovered = False
        self._phase = 0.0
        self.setFixedHeight(MIN_H)
        self.setCursor(Qt.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._reveal = 0.0            # hover "relax": the pill grows/brightens in
        self._reveal_target = 0.0
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setInterval(16)
        self._reveal_timer.timeout.connect(self._reveal_step)

    def set_state(self, state: str) -> None:
        self._state = state
        # Breathe while a task is live (working) or blocked on you (needs_you) — both show at rest
        # so you always know something's running, even before the first step or an approval.
        if state in ("working", "needs_you"):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def set_hovered(self, hovered: bool) -> None:
        if hovered != self._hovered:
            self._hovered = hovered
            self.update()

    def enterEvent(self, e) -> None:
        self.set_hovered(True)
        self._reveal_target = 1.0
        if not self._reveal_timer.isActive():
            self._reveal_timer.start()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.set_hovered(False)
        self._reveal_target = 0.0
        if not self._reveal_timer.isActive():
            self._reveal_timer.start()
        super().leaveEvent(e)

    def _reveal_step(self) -> None:
        d = self._reveal_target - self._reveal
        if abs(d) < 0.02:
            self._reveal = self._reveal_target
            self._reveal_timer.stop()
        else:
            self._reveal += d * 0.28
        self.update()

    def _tick(self) -> None:
        self._phase += 0.14
        self.update()

    def _shows_at_rest(self) -> bool:
        # Visible unless truly IDLE: a running task (working, breathing purple), a blocked approval
        # (amber), a finished task (green), or an error (red). Only idle is invisible.
        return self._state in ("working", "needs_you", "done", "error")

    def _color(self) -> QColor:
        base = _STATE_COLOR.get(self._state, _STATE_COLOR["idle"])
        if self._hovered and self._state in ("idle", "working", "done"):
            base = QColor(255, 255, 255, 150)      # neutral affordance on hover
        if self._state in ("working", "needs_you") and not self._hovered:
            a = 150 + int(90 * (0.5 + 0.5 * math.sin(self._phase)))
            return QColor(base.red(), base.green(), base.blue(), a)
        return base

    def paintEvent(self, _e) -> None:
        base = self._shows_at_rest()
        if not base and self._reveal < 0.02:
            return                                   # truly idle: invisible, nothing to see
        r = max(self._reveal, 0.45) if base else self._reveal   # states keep a visible baseline
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pill_w = 30.0 + 34.0 * r                     # "relax": grows a bit on hover
        pill_h = 5.0
        x = (self.width() - pill_w) / 2.0
        y = 2.0                                      # hug the notch's bottom edge
        col = self._color()
        if not base:                                 # fade the hover pill in with the reveal
            col = QColor(col.red(), col.green(), col.blue(), int(col.alpha() * min(1.0, 0.4 + r)))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(x, y, pill_w, pill_h), pill_h / 2.0, pill_h / 2.0)
        if self._state == "needs_you":
            p.drawEllipse(QPointF(x + pill_w + 9, y + pill_h / 2.0), 3.5, 3.5)
        p.end()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()
        e.accept()


class _Spinner(QWidget):
    """Small rotating arc for the working page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0.0
        self.setFixedSize(26, 26)
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
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        pen = QPen(QColor(ACCENT), 3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(-self._angle * 16), 270 * 16)
        p.end()


class ClaudeOverlay(QWidget):
    """Ambient Assistant notch. Rests as a handle; hover expands; never auto-drops."""

    approve_clicked = Signal()
    deny_clicked = Signal()
    open_requested = Signal()     # "Open in Claude Code / view full task"
    dismissed = Signal()
    model_changed = Signal(str, str)   # (model, effort) picked on the card's model pill
    stop_clicked = Signal()            # "Stop" pressed while a task is running

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(WIN_TITLE)
        self.setFixedWidth(WIDGET_W)
        # System font (like organize) — hand-tuned heights must not shift under the app font.
        self.setStyleSheet("QWidget { font-family: '-apple-system', 'SF Pro Display', "
                           "'Helvetica Neue', 'Segoe UI', sans-serif; }")

        self._state = "idle"
        self._minimized = True
        self._pinned = False          # click the handle to keep it open
        self._anim = None
        self._task_text = ""          # the prompt we're running — shown on the done page
        self._result_text = ""        # what Claude generated — shown on the done page
        self._details_open = False    # is the live "Details" feed expanded?
        self._detail_last = ""        # last line appended to the feed (dedup)
        self._model = "opus"          # a real model is always chosen: opus/sonnet/haiku
        self._effort = "high"         # a real effort is always chosen: low/medium/high/xhigh/max
        self._model_open = False      # is the inline "Model & effort" panel expanded?
        self._model_list_open = False # is the model dropdown list expanded?

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._maybe_collapse)
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self.dismiss)
        self._flash_timer = QTimer(self)      # briefly-open "receipt", then tuck away
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._elapsed_timer = QTimer(self)    # "Working 2:26" counter in the header
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._task_started = None

        self._build_ui()

    # -- construction ----------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, SHADOW_MARGIN)

        self._card = QFrame()
        self._card.setObjectName("claudeCard")
        # Rounded on all corners: it hangs just BELOW the notch now (not flush to the top edge),
        # so a square top read as a detached slab. A full rounded card looks intentional/centred.
        # Square top (flush with the notch, merges with it), rounded bottom — it drops FROM the notch.
        self._card_style = (
            f"QFrame#claudeCard {{ background-color: {CARD_BG}; border: none; "
            "border-top-left-radius: 0px; border-top-right-radius: 0px; "
            "border-bottom-left-radius: 20px; border-bottom-right-radius: 20px; }"
        )
        self._card.setStyleSheet(self._card_style)
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 170))
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        col = QVBoxLayout(self._card)
        col.setContentsMargins(16, 12, 16, 12)
        col.setSpacing(9)

        # Transcript bubble (VoiceOS-style): "what you said", a lighter rounded box above the card.
        self._transcript = QLabel("")
        self._transcript.setWordWrap(True)
        self._transcript.setMaximumHeight(TRANSCRIPT_H)
        self._transcript.setStyleSheet(
            f"QLabel {{ background: {C['surface']}; color: {C['text_2']}; font-size: 12.5px; "
            "border-radius: 13px; padding: 9px 13px; }")
        self._transcript.setVisible(False)
        col.addWidget(self._transcript)

        # The "Claude Code" card sits on the black backing — give it a subtly lighter inset look.
        # Header (VoiceOS-style): [icon] Claude Code  ···  <status> <timer>  [⇣] [✕]
        self._topbar = QWidget()
        self._topbar.setFixedHeight(TOPBAR_H)
        self._topbar.setStyleSheet("background: transparent;")
        top = QHBoxLayout(self._topbar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        self._icon.setAlignment(Qt.AlignCenter)
        _logo = _load_logo(20)
        if _logo is not None:
            self._icon.setPixmap(_logo)
            self._icon.setStyleSheet("background: transparent; border: none;")
        else:
            self._icon.setText("✦")   # neutral placeholder until a real logo is dropped in assets/
            self._icon.setStyleSheet(
                f"color: {ACCENT}; font-size: 16px; font-weight: 800; background: transparent; border: none;")
        top.addWidget(self._icon, 0, Qt.AlignVCenter)
        self._brand = QLabel("Claude Code")
        self._brand.setStyleSheet(
            f"color: {C['text']}; font-size: 13px; font-weight: 600; background: transparent; border: none;")
        top.addWidget(self._brand, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self._status_label = QLabel("")        # Working / Needs you / Done / Error
        self._status_label.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        top.addWidget(self._status_label, 0, Qt.AlignVCenter)
        self._timer_label = QLabel("")         # elapsed, e.g. 2:26
        self._timer_label.setStyleSheet(
            f"color: {C['text_2']}; font-size: 12px; background: transparent; border: none;")
        top.addWidget(self._timer_label, 0, Qt.AlignVCenter)
        top.addSpacing(4)
        self._pin_btn = QPushButton("⇣")
        self._pin_btn.setToolTip("Tuck away")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setFixedSize(22, 22)
        self._pin_btn.setStyleSheet(self._icon_btn_style())
        self._pin_btn.clicked.connect(self._toggle_pin)
        top.addWidget(self._pin_btn, 0, Qt.AlignVCenter)
        close = QPushButton("✕")
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close")
        close.setFixedSize(22, 22)
        close.setStyleSheet(self._icon_btn_style())
        close.clicked.connect(self._on_dismiss)
        top.addWidget(close, 0, Qt.AlignVCenter)
        col.addWidget(self._topbar)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._page_working = self._make_working_page()    # 0
        self._page_needs = self._make_needs_page()         # 1
        self._page_done = self._make_message_page("done")  # 2
        self._page_error = self._make_message_page("error")  # 3
        for pg in (self._page_working, self._page_needs, self._page_done, self._page_error):
            self._stack.addWidget(pg)
        col.addWidget(self._stack)

        # Collapsible live feed — card-level so Details works in any state.
        self._details_feed = QTextEdit()
        self._details_feed.setReadOnly(True)
        self._details_feed.setFixedHeight(DETAILS_H - 6)
        self._details_feed.setVisible(False)
        self._details_feed.setStyleSheet(
            f"QTextEdit {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px; "
            f"color: {C['text_2']}; font-size: 11.5px; padding: 8px; }}")
        col.addWidget(self._details_feed)

        # Persistent control row: [model·effort pill]   [Details]  [primary action]
        self._ctrl_row = QWidget()
        self._ctrl_row.setFixedHeight(CTRL_H)
        self._ctrl_row.setStyleSheet("background: transparent;")
        ctrl = QHBoxLayout(self._ctrl_row)
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setSpacing(8)
        self._model_btn = QPushButton("Auto  ⌄")     # model · thinking-effort picker
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.setStyleSheet(
            f"QPushButton {{ background: {C['surface']}; border: 1px solid {C['border']}; "
            f"color: {C['text_2']}; font-size: 11.5px; font-weight: 600; border-radius: 8px; padding: 3px 10px; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; color: {C['text']}; }}")
        self._model_btn.clicked.connect(self._toggle_model_panel)
        ctrl.addWidget(self._model_btn)
        ctrl.addStretch(1)
        self._details_btn = QPushButton("Details  ⌄")
        self._details_btn.setCursor(Qt.PointingHandCursor)
        self._details_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {C['text_muted']}; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {C['text']}; }}")
        self._details_btn.clicked.connect(self._toggle_details)
        ctrl.addWidget(self._details_btn)
        self._primary_btn = QPushButton("")          # Stop (working) / Open in Claude (done/error)
        self._primary_btn.setCursor(Qt.PointingHandCursor)
        self._primary_btn.setFixedHeight(26)
        self._primary_btn.clicked.connect(self._on_primary)
        ctrl.addWidget(self._primary_btn)
        col.addWidget(self._ctrl_row)

        # Inline "Model & effort" panel — opens below the controls when the model pill is tapped.
        # Inline (not a QMenu/popup) so it RELIABLY receives clicks from the non-activating notch
        # panel (a popup can't become key there, which is why the old dropdown did nothing).
        self._model_panel = QFrame()
        self._model_panel.setObjectName("modelPanel")      # targeted bg so it reads as a dark box
        self._model_panel.setStyleSheet(
            f"QFrame#modelPanel {{ background:#1E1E24; border:1px solid {C['border']}; border-radius:12px; }}")
        self._model_panel.setVisible(False)
        mp = QVBoxLayout(self._model_panel)
        mp.setContentsMargins(14, 12, 14, 12)
        mp.setSpacing(6)
        _title = QLabel("Model & effort")
        _title.setStyleSheet(f"color:{C['text']}; font-size:12.5px; font-weight:700; background:transparent; border:none;")
        mp.addWidget(_title)
        _mlbl = QLabel("Model")
        _mlbl.setStyleSheet(f"color:{C['text_muted']}; font-size:10.5px; font-weight:600; background:transparent; border:none;")
        mp.addWidget(_mlbl)
        # Model DROPDOWN (box + inline list) — like VoiceOS. Inline so it works on the notch panel.
        self._model_select = QPushButton("Opus")
        self._model_select.setCursor(Qt.PointingHandCursor)
        self._model_select.setFixedHeight(32)
        self._model_select.setStyleSheet(
            f"QPushButton {{ background:#202026; color:{C['text']}; border:1px solid {C['border']}; "
            "border-radius:9px; font-size:12.5px; font-weight:600; text-align:left; padding:0 12px; }"
            f"QPushButton:hover {{ border-color:{ACCENT}; }}")
        self._model_select.clicked.connect(self._toggle_model_list)
        mp.addWidget(self._model_select)
        self._model_list = QWidget()
        self._model_list.setStyleSheet("background:transparent;")
        self._model_list.setVisible(False)
        ml = QVBoxLayout(self._model_list)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(3)
        self._model_rows = {}
        for k in _MODELS:
            r = QPushButton(_MODEL_NAMES[k])
            r.setCursor(Qt.PointingHandCursor)
            r.setFixedHeight(28)
            r.setStyleSheet(
                f"QPushButton {{ background:#202026; color:{C['text_2']}; border:1px solid {C['border']}; "
                "border-radius:8px; font-size:12px; text-align:left; padding:0 12px; }"
                f"QPushButton:hover {{ background:{C['surface']}; color:{C['text']}; border-color:{ACCENT}; }}")
            r.clicked.connect(lambda _=False, kk=k: self._pick_model(kk))
            self._model_rows[k] = r
            ml.addWidget(r)
        mp.addWidget(self._model_list)
        erow = QHBoxLayout()
        _elbl = QLabel("Effort")
        _elbl.setStyleSheet(f"color:{C['text_muted']}; font-size:10.5px; font-weight:600; background:transparent; border:none;")
        erow.addWidget(_elbl)
        self._effort_name = QLabel("High")
        self._effort_name.setStyleSheet(f"color:{ACCENT}; font-size:11.5px; font-weight:700; background:transparent; border:none;")
        erow.addSpacing(8)
        erow.addWidget(self._effort_name)
        erow.addStretch(1)
        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setCursor(Qt.PointingHandCursor)
        self._auto_btn.setFixedHeight(22)
        self._auto_btn.clicked.connect(self._toggle_auto_effort)
        erow.addWidget(self._auto_btn)
        mp.addLayout(erow)
        self._effort_slider = QSlider(Qt.Horizontal)
        self._effort_slider.setRange(0, 4)
        self._effort_slider.setPageStep(1)
        self._effort_slider.setFixedHeight(18)
        # No tick marks: enabling them made Qt draw a white groove frame (the rendering bug).
        self._effort_slider.valueChanged.connect(self._on_effort_slider)
        mp.addWidget(self._effort_slider)
        ends = QHBoxLayout()
        _f = QLabel("Faster")
        _s = QLabel("Smarter")
        for _e in (_f, _s):
            _e.setStyleSheet(f"color:{C['text_muted']}; font-size:9.5px; background:transparent; border:none;")
        ends.addWidget(_f)
        ends.addStretch(1)
        ends.addWidget(_s)
        mp.addLayout(ends)
        col.addWidget(self._model_panel)

        # Footer: keycap shortcut hint (VoiceOS-style), subtle, below the content.
        self._footer = QLabel()
        self._footer.setFixedHeight(FOOTER_H)
        self._footer.setTextFormat(Qt.RichText)
        self._footer.setText(self._hint_html())
        self._footer.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 11px; background: transparent; border: none;")
        col.addWidget(self._footer)

        # Resting handle (shown when minimized; the card is hidden then).
        self._handle = _Handle()
        self._handle.clicked.connect(self._on_handle_clicked)
        col.addWidget(self._handle)

        self._sync_model_panel()        # initial model-button highlight + slider position
        self._apply_minimized_chrome(True)

    def _hint_html(self) -> str:
        chip = (f"background:{C['surface']}; border:1px solid {C['border']}; border-radius:4px; "
                "padding:1px 5px; font-size:10px;")
        return (f"Hold <span style='{chip}'>fn</span> "
                f"<span style='{chip}'>⌃ control</span> to speak")

    def _icon_btn_style(self) -> str:
        return (
            "QPushButton { background: transparent; border: none; "
            f"color: {C['text_muted']}; font-size: 12px; border-radius: 11px; }}"
            f"QPushButton:hover {{ background: {C['surface']}; color: {C['text']}; }}")

    def _make_working_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._work_spinner = _Spinner()          # kept (hidden) — the header status conveys activity
        self._work_spinner.setVisible(False)
        self._work_label = QLabel("")            # kept for compatibility; NOT shown (the transcript
        self._work_label.setVisible(False)       # bubble at the top is the task — no duplicate title)
        # Current ACTION (muted), one line — the only thing the working page shows; the task is the
        # bubble, the full activity is in Details. No stretch here (it caused a big empty gap).
        self._tool_chip = QLabel("")
        self._tool_chip.setWordWrap(False)
        self._tool_chip.setStyleSheet(
            f"color: {C['text_muted']}; font-size: 12.5px; background: transparent; border: none;")
        lay.addWidget(self._tool_chip, 0, Qt.AlignVCenter)
        return pg

    def _make_needs_page(self) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self._needs_title = QLabel("Claude wants to run a tool")
        self._needs_title.setWordWrap(True)
        self._needs_title.setStyleSheet(
            f"color: {C['text']}; font-size: 14px; font-weight: 700; background: transparent; border: none;")
        lay.addWidget(self._needs_title)
        self._needs_detail = QLabel("")
        self._needs_detail.setWordWrap(True)
        self._needs_detail.setMaximumHeight(60)         # cap — full args live in the Activity feed
        self._needs_detail.setStyleSheet(
            f"color: {C['text_2']}; font-size: 11px; font-family: monospace; "
            "background: transparent; border: none;")
        lay.addWidget(self._needs_detail)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        deny = QPushButton("Deny")
        deny.setCursor(Qt.PointingHandCursor)
        deny.setFixedHeight(34)
        deny.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {RED}; border: 1px solid {RED}; "
            "border-radius: 9px; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(255,107,107,0.10); }")
        deny.clicked.connect(self._on_deny)
        btns.addWidget(deny, 1)
        approve = QPushButton("Approve")
        approve.setCursor(Qt.PointingHandCursor)
        approve.setFixedHeight(34)
        approve.setStyleSheet(
            f"QPushButton {{ background: {GREEN}; color: white; border: none; "
            "border-radius: 9px; font-size: 13px; font-weight: 700; }"
            "QPushButton:hover { background: #278238; }")
        approve.clicked.connect(self._on_approve)
        btns.addWidget(approve, 1)
        lay.addLayout(btns)
        return pg

    def _make_message_page(self, kind: str) -> QWidget:
        pg = QWidget()
        pg.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(pg)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        glyph = QLabel("✓" if kind == "done" else "⚠")
        glyph.setStyleSheet(
            f"color: {GREEN if kind == 'done' else RED}; font-size: 16px; font-weight: 700; "
            "background: transparent; border: none;")
        row.addWidget(glyph, 0, Qt.AlignTop)
        label = QLabel("")
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {C['text']}; font-size: 13px; background: transparent; border: none;")
        row.addWidget(label, 1)
        lay.addLayout(row)
        if kind == "done":
            self._done_label = label          # "Open in Claude" is the persistent primary button now
        else:
            self._error_label = label
        lay.addStretch(1)
        return pg

    # -- public API (called from the UI thread via AssistantPage slots) --------
    def present(self) -> None:
        """Start a task: drop the panel under the notch. It then expands (via flash) and STAYS up
        for the whole task — pinned, so it doesn't tuck to a pill or collapse when the mouse leaves
        (you asked for it constantly up). You tuck it via the ⇣ button or dismiss with ✕."""
        self._done_timer.stop()
        self._pinned = True          # stay open through routing → working → approval → done
        self._sync_pin_btn()
        self.set_state("working")
        self._set_status("Working", C["text_muted"])
        self._start_elapsed()        # reset the "Working m:ss" counter for this task
        self._reset_details()        # fresh live feed, collapsed, for the new task
        # Already up (e.g. the "searching" card -> the task starting): keep it up, just re-centre —
        # don't collapse to a pill and re-expand, which flickers between routing and the task.
        if self.isVisible() and not self._minimized:
            self._reanchor()
            self._reassert()
            return
        self._minimized = True
        self._apply_minimized_chrome(True)
        self.setFixedWidth(STRIP_W)          # rest as the narrow strip, not full card width
        geo = notch_panel.screen_geo()
        if geo is None:
            if not self.isVisible():
                self.show()
            self._configure_macos_deferred()
            return
        self.resize(STRIP_W, self._min_h())
        x = geo.x() + (geo.width() - STRIP_W) // 2
        self.move(x, geo.y() - self._min_h())    # start just above the top edge, then slide down
        if not self.isVisible():
            self.show()
        self._configure_macos_deferred()
        self._slide_to(x, self._rest_y(geo))

    def set_title(self, title: str) -> None:
        """Sets the transcript bubble ("what you said"). That IS the task display — there is no
        separate bold title in the card, so the prompt is never shown twice."""
        title = (title or "").strip()
        if title:
            self._transcript.setText(title[:300])
            if not self._minimized:
                self._transcript.setVisible(True)
        if title and not self._details_feed.toPlainText().strip():
            self._append_detail(f"▸ {title}")      # seed the Details feed with the task
        if not self._minimized:
            self._fit()

    def show_working(self, text: str = "", tool: str = "") -> None:
        self.set_state("working")
        self._set_status("Working", C["text_muted"])
        if not self._elapsed_timer.isActive():
            self._start_elapsed()
        action = text.strip() if text else (f"Using {tool}…" if tool else "")
        self._tool_chip.setText(action[:160])
        self._tool_chip.setVisible(bool(action))
        if action:
            self._append_detail(action)            # accumulate into the live Details feed
        self._stack.setCurrentWidget(self._page_working)
        if not self._minimized:
            self._fit()
        if self._flash_timer.isActive():
            self._flash_timer.start(FLASH_MS)

    # -- live "Details" feed (collapsible) -------------------------------------
    def _append_detail(self, line: str) -> None:
        line = (line or "").strip()
        if not line or line == self._detail_last:
            return
        self._detail_last = line
        self._details_feed.append(line)
        sb = self._details_feed.verticalScrollBar()
        sb.setValue(sb.maximum())                  # keep the newest line in view

    def _reset_details(self) -> None:
        self._details_open = False
        self._detail_last = ""
        self._details_feed.clear()
        self._details_feed.setVisible(False)
        self._details_btn.setText("Details  ⌄")

    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        self._details_feed.setVisible(self._details_open)
        self._details_btn.setText("Show less  ⌃" if self._details_open else "Details  ⌄")
        sb = self._details_feed.verticalScrollBar()
        sb.setValue(sb.maximum())
        if not self._minimized:
            self._fit()

    # -- model + thinking-effort picker (inline; reliable on the non-activating panel) ----
    def set_model_effort(self, model: str, effort: str) -> None:
        self._model = (model or "").strip() or "opus"       # always a real model
        self._effort = (effort or "").strip()               # '' = Auto; else a slider level
        if self._effort and self._effort not in _EFFORT_ORDER:
            self._effort = "high"
        self._sync_model_panel()
        self._update_model_pill()

    def _update_model_pill(self) -> None:
        ml = _MODEL_NAMES.get(self._model, "Opus")
        el = _EFFORT_NAMES.get(self._effort, "High")
        self._model_btn.setText(f"{ml} · {el}  ⌄")

    def _toggle_model_panel(self) -> None:
        self._model_open = not self._model_open
        if not self._model_open:
            self._model_list_open = False
            self._model_list.setVisible(False)
        self._model_panel.setVisible(self._model_open)
        if not self._minimized:
            self._fit()

    def _toggle_model_list(self) -> None:
        self._model_list_open = not self._model_list_open
        self._model_list.setVisible(self._model_list_open)
        self._sync_model_panel()
        if not self._minimized:
            self._fit()

    def _pick_model(self, k: str) -> None:
        self._model = k
        self._model_list_open = False
        self._model_list.setVisible(False)
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)
        if not self._minimized:
            self._fit()

    def _sync_model_panel(self) -> None:
        """Reflect the current model/effort on the dropdown, slider, and Auto button."""
        chev = "  ⌃" if self._model_list_open else "  ⌄"
        self._model_select.setText(_MODEL_NAMES.get(self._model, "Opus") + chev)
        for k, r in getattr(self, "_model_rows", {}).items():
            r.setVisible(k != self._model)        # don't list the already-selected model
        auto = (self._effort == "")
        idx = _EFFORT_ORDER.index(self._effort) if self._effort in _EFFORT_ORDER else 2
        self._effort_slider.blockSignals(True)
        self._effort_slider.setValue(idx)
        self._effort_slider.blockSignals(False)
        fill = "#4A4A52" if auto else ACCENT              # grey fill when Auto (not manually set)
        self._effort_slider.setStyleSheet(
            "QSlider { background:transparent; border:none; }"
            "QSlider::groove:horizontal { border:none; height:4px; background:#2A2A30; border-radius:2px; }"
            f"QSlider::sub-page:horizontal {{ background:{fill}; border-radius:2px; }}"
            "QSlider::add-page:horizontal { border:none; background:#2A2A30; border-radius:2px; }"
            "QSlider::handle:horizontal { background:white; border:none; width:14px; height:14px; "
            "margin:-6px 0; border-radius:7px; }")
        self._effort_name.setText(_EFFORT_NAMES.get(self._effort, "High"))
        if auto:
            self._auto_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:none; border-radius:11px; "
                f"font-size:10.5px; font-weight:600; padding:0 10px; }}")
        else:
            self._auto_btn.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{C['text_muted']}; "
                f"border:1px solid {C['border']}; border-radius:11px; font-size:10.5px; font-weight:600; padding:0 10px; }}"
                f"QPushButton:hover {{ border-color:{ACCENT}; color:{C['text']}; }}")

    def _set_model(self, k: str) -> None:
        self._model = k
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)

    def _toggle_auto_effort(self) -> None:
        """Auto toggles: on -> off switches back to the slider's current level (so you can uncheck it)."""
        if self._effort == "":
            idx = self._effort_slider.value()
            self._set_effort(_EFFORT_ORDER[max(0, min(len(_EFFORT_ORDER) - 1, idx))])
        else:
            self._set_effort("")

    def _on_effort_slider(self, v: int) -> None:
        self._set_effort(_EFFORT_ORDER[max(0, min(len(_EFFORT_ORDER) - 1, v))])

    def _set_effort(self, k: str) -> None:
        self._effort = k
        self._sync_model_panel()
        self._update_model_pill()
        self.model_changed.emit(self._model, self._effort)

    def show_needs_you(self, tool: str, tool_input: dict | None = None) -> None:
        """A tool call is gated — the task is BLOCKED until Approve/Deny."""
        self.set_state("needs_you")
        self._set_status("Needs you", AMBER)
        self._needs_title.setText(f"Run  {tool}?")
        detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in (tool_input or {}).items())
        self._needs_detail.setText(detail or "(no details)")
        self._stack.setCurrentWidget(self._page_needs)
        if not self._minimized:
            self._fit()

    def show_done(self, session_id: str = "", cost: float = 0.0, task: str = "", result: str = "") -> None:
        self.set_state("done")
        self._set_status("Done", GREEN)
        self._stop_elapsed()
        self._work_spinner.stop()
        if task:
            self._task_text = task
        if result:
            self._result_text = result
        sid = (session_id or "")[:8]
        meta = "✓ Finished"
        if sid:
            meta += f"  ·  saved {sid}…"
        if cost:
            meta += f"  ·  ${cost:.2f}"
        # Lead with what Claude GENERATED (the result), not just "Finished" — that's what you want
        # to glance at. Full output is behind "View task →". Fall back to the prompt if no text.
        shown = self._result_text.strip() or self._task_text.strip()
        body = (shown[:240] + ("…\n\n" if len(shown) > 240 else "\n\n")) if shown else ""
        self._done_label.setText(body + meta)
        self._stack.setCurrentWidget(self._page_done)
        # Stay up showing the result (pinned since the task started) — dismiss with ✕, or tuck it to
        # a reopenable green pill with the ⇣ button. (If already tucked, it rests as the green pill.)
        if not self._minimized:
            self._fit()

    def show_error(self, msg: str) -> None:
        self.set_state("error")
        self._set_status("Error", RED)
        self._stop_elapsed()
        self._work_spinner.stop()
        self._done_timer.stop()
        self._error_label.setText((msg or "Something went wrong.")[:200])
        self._stack.setCurrentWidget(self._page_error)
        if not self._minimized:
            self._fit()

    def set_state(self, state: str) -> None:
        self._state = state
        self._handle.set_state(state)
        self._update_primary()

    # -- primary action: Stop while working / Open in Claude when done ----------
    def _on_primary(self) -> None:
        if self._state == "working":
            self.stop_clicked.emit()
        else:
            self.open_requested.emit()

    def mark_stopping(self) -> None:
        """Called the instant Stop is clicked: disable the button + show it registered, so there's
        no reason to click it twice."""
        self._set_status("Stopping…", C["text_muted"])
        self._primary_btn.setText("Stopping…")
        self._primary_btn.setEnabled(False)
        self._primary_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {C['text_muted']}; "
            f"border: 1px solid {C['border']}; border-radius: 8px; font-size: 12px; font-weight: 600; padding: 0 12px; }}")

    def _update_primary(self) -> None:
        self._primary_btn.setEnabled(True)          # re-enable after a stop/state change
        if self._state == "working":
            self._primary_btn.setText("◼  Stop")
            self._primary_btn.setVisible(True)
            self._primary_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {RED}; border: 1px solid {RED}; "
                "border-radius: 8px; font-size: 12px; font-weight: 600; padding: 0 12px; }"
                "QPushButton:hover { background: rgba(242,100,90,0.12); }")
        elif self._state in ("done", "error"):
            self._primary_btn.setText("Open in Claude  →")
            self._primary_btn.setVisible(True)
            self._primary_btn.setStyleSheet(
                f"QPushButton {{ background: {C['surface']}; color: {C['text']}; border: 1px solid {C['border']}; "
                "border-radius: 8px; font-size: 12px; font-weight: 600; padding: 0 12px; }"
                f"QPushButton:hover {{ background: {ACCENT}; color: white; border-color: {ACCENT}; }}")
        else:                                   # needs_you — Approve/Deny live in the page
            self._primary_btn.setVisible(False)

    # -- header status + elapsed timer -----------------------------------------
    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; font-weight: 600; background: transparent; border: none;")

    def _start_elapsed(self) -> None:
        import time
        self._task_started = time.monotonic()
        self._tick_elapsed()
        if not self._elapsed_timer.isActive():
            self._elapsed_timer.start()

    def _stop_elapsed(self) -> None:
        self._elapsed_timer.stop()      # keep the final time shown

    def _tick_elapsed(self) -> None:
        if self._task_started is None:
            self._timer_label.setText("")
            return
        import time
        s = int(max(0, time.monotonic() - self._task_started))
        self._timer_label.setText(f"{s // 60}:{s % 60:02d}")

    def flash(self, ms: int = FLASH_MS) -> None:
        """Briefly drop the panel open — a 'receipt' that a (voice) task started, or that one is
        already running — then tuck back to the invisible handle unless you hovered or pinned it."""
        self._collapse_timer.stop()
        if self._minimized:
            self.restore_panel()
        self._flash_timer.start(ms)

    def _end_flash(self) -> None:
        if self._pinned:
            return
        try:
            if self.frameGeometry().contains(QCursor.pos()):
                self._flash_timer.start(700)      # still hovering — keep it open
                return
        except Exception:
            pass
        if not self._minimized:
            self.minimize_panel()

    def dismiss(self) -> None:
        """Slide up and hide."""
        self._done_timer.stop()
        self._flash_timer.stop()
        self._collapse_timer.stop()
        self._stop_anim()
        self._work_spinner.stop()
        geo = notch_panel.screen_geo()
        if geo is None or not self.isVisible():
            self.hide()
            return
        self._slide_to(self.x(), geo.y() - self.height(), on_done=self.hide)

    # -- hover / expand / collapse --------------------------------------------
    def enterEvent(self, e) -> None:
        # Hover only REVEALS the faint pill (so you can see where to click) — it does NOT open.
        self._collapse_timer.stop()
        if self._minimized:
            self._handle.set_hovered(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if self._minimized:
            self._handle.set_hovered(False)
        elif not self._pinned:
            self._collapse_timer.start(400)       # expanded: collapse shortly after you leave
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        # A click anywhere on the resting hotspot opens the panel (and pins it).
        if self._minimized:
            self._on_handle_clicked()
            e.accept()
            return
        # Finished/errored: a click on the card body TUCKS it back into the notch (a small indicator
        # stays there — click the notch to reopen and see the result). ✕ still fully dismisses. Clicks
        # on the buttons (Open in Claude Code, ✕, ⇣) are consumed by them first, so this only fires
        # on the card body.
        if self._state in ("done", "error"):
            self.minimize_panel()
            e.accept()
            return
        super().mousePressEvent(e)

    def _maybe_collapse(self) -> None:
        if self._pinned or self._minimized:
            return
        try:
            if self.frameGeometry().contains(QCursor.pos()):
                # Still inside (likely over a child widget — Qt leaveEvent gotcha). Re-check soon.
                self._collapse_timer.start(300)
                return
        except Exception:
            pass
        self.minimize_panel()

    def _on_handle_clicked(self) -> None:
        # Tapping the handle expands AND pins, so it stays put while you read/decide.
        self._pinned = True
        self._sync_pin_btn()
        if self._minimized:
            self.restore_panel()

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self._sync_pin_btn()
        if not self._pinned and not self.frameGeometry().contains(QCursor.pos()):
            self._collapse_timer.start(200)

    def _sync_pin_btn(self) -> None:
        self._pin_btn.setText("⇣" if self._pinned else "⇡")
        self._pin_btn.setToolTip("Unpin (collapse on leave)" if self._pinned else "Keep open")

    def restore_panel(self) -> None:
        """Expand from the handle to the full card."""
        self._minimized = False
        self._apply_minimized_chrome(False)
        self.setFixedWidth(WIDGET_W)
        self._fit()
        self._reanchor()
        self._reassert()

    def minimize_panel(self) -> None:
        """Collapse the card back to the resting handle."""
        if self._minimized:
            return
        self._minimized = True
        self._apply_minimized_chrome(True)
        self.setFixedWidth(STRIP_W)
        self.setMinimumHeight(0)
        self.resize(STRIP_W, self._min_h())
        self._reanchor()
        self._reassert()

    def _apply_minimized_chrome(self, minimized: bool) -> None:
        """Show just the handle (minimized) or the full card (expanded). When minimized we also
        collapse the margins so the resting window is ~20px tall — a tiny transparent strip that
        hugs the notch, instead of a tall invisible box that would eat clicks at top-centre."""
        self._topbar.setVisible(not minimized)
        self._stack.setVisible(not minimized)
        self._ctrl_row.setVisible(not minimized)
        self._footer.setVisible(not minimized)
        self._transcript.setVisible((not minimized) and bool(self._transcript.text()))
        self._details_feed.setVisible((not minimized) and self._details_open)
        if minimized:
            self._model_open = False
            self._model_list_open = False
            self._model_list.setVisible(False)
        self._model_panel.setVisible((not minimized) and self._model_open)
        self._handle.setVisible(minimized)
        col = self._card.layout()
        eff = self._card.graphicsEffect()
        nb = notch_panel.notch_inset_top()      # the notch/menu-bar band the card top sits behind
        if minimized:
            # A small black nub that MERGES with the notch (black top band behind it) with the
            # resting indicator hanging just below the notch's bottom edge.
            self.layout().setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, 2)
            col.setContentsMargins(16, nb, 16, 1)
            self._card.setStyleSheet(self._nub_style())
            if eff is not None:
                eff.setEnabled(False)
        else:
            self.layout().setContentsMargins(SHADOW_MARGIN, 0, SHADOW_MARGIN, SHADOW_MARGIN)
            col.setContentsMargins(16, nb + 8, 16, 12)    # content starts BELOW the notch band
            self._card.setStyleSheet(self._card_style)
            if eff is not None:
                eff.setEnabled(True)

    def _nub_style(self) -> str:
        # The resting nub: same near-black as the card (merges with the notch), rounded only at the
        # bottom so it looks like a small tongue hanging from the notch.
        return (f"QFrame#claudeCard {{ background-color: {CARD_BG}; border: none; "
                "border-top-left-radius: 0px; border-top-right-radius: 0px; "
                "border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }")

    # -- sizing / anchoring ----------------------------------------------------
    def _content_height(self) -> int:
        return _STACK_H.get(self._state, 90)

    def _fit(self) -> None:
        col = self._card.layout()
        cm = col.contentsMargins()
        om = self.layout().contentsMargins()
        sp = col.spacing()
        h = om.top() + cm.top()
        if self._transcript.text():
            h += TRANSCRIPT_H + sp            # the "what you said" bubble
        self._stack.setFixedHeight(self._content_height())   # pin it so the page doesn't expand
        h += TOPBAR_H + sp + self._content_height() + sp + CTRL_H
        if self._model_open:
            h += sp + MODELPANEL_H            # the inline model & effort panel
            if self._model_list_open:
                h += MODEL_LIST_H             # model dropdown list expanded
        if self._details_open:
            h += sp + (DETAILS_H - 6)         # the expanded live feed
        h += sp + FOOTER_H + cm.bottom() + om.bottom()
        self.setMinimumHeight(0)
        self.resize(self.width(), max(60, int(h)))
        self._reassert()

    def _rest_y(self, geo) -> int:
        # Anchor the window AT the top edge so the card's black top band merges with the physical
        # notch (VoiceOS-style: it looks like it lives in/drops from the notch). Content is pushed
        # below the notch via the card's top margin (= the notch band), so nothing hides behind it.
        return geo.y() + TOP_MARGIN

    def _min_h(self) -> int:
        # Minimized window height: the notch band + a small strip for the resting indicator.
        return notch_panel.notch_inset_top() + MIN_H

    def _reanchor(self) -> None:
        # Cancel any in-flight present() slide first — it ends at the pill-width centre and would
        # otherwise override this move, leaving an expanded card off-centre (the "searching card
        # not centred" bug). Use the exact width for the state so centring is deterministic.
        self._stop_anim()
        if not self.isVisible():
            return
        geo = notch_panel.screen_geo()
        if geo is None:
            return
        w = STRIP_W if self._minimized else WIDGET_W
        x = geo.x() + (geo.width() - w) // 2
        self.move(x, self._rest_y(geo))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._reassert()

    def _reassert(self) -> None:
        notch_panel.reassert_panel_style(WIN_TITLE)

    def _slide_to(self, x: int, y: int, on_done=None) -> None:
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(170)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setEndValue(QPointF(x, y).toPoint())
        if on_done is not None:
            anim.finished.connect(on_done)
        self._anim = anim
        anim.start()

    def _stop_anim(self) -> None:
        if self._anim is not None:
            try:
                self._anim.stop()
            except Exception:
                pass

    def _configure_macos_deferred(self) -> None:
        if sys.platform != "darwin":
            return
        notch_panel.configure_macos_panel(WIN_TITLE)
        QTimer.singleShot(10, lambda: notch_panel.configure_macos_panel(WIN_TITLE))
        QTimer.singleShot(60, lambda: notch_panel.configure_macos_panel(WIN_TITLE))

    # -- decisions / dismiss ---------------------------------------------------
    def _on_approve(self) -> None:
        self.approve_clicked.emit()
        self.show_working("Approved — continuing…")

    def _on_deny(self) -> None:
        self.deny_clicked.emit()
        self.show_working("Denied.")

    def _on_dismiss(self) -> None:
        self.dismissed.emit()
        self.dismiss()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self._on_dismiss()
            return
        super().keyPressEvent(event)


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    ov = ClaudeOverlay()

    fired = {"approve": 0, "deny": 0, "open": 0, "dismiss": 0}
    ov.approve_clicked.connect(lambda: fired.__setitem__("approve", fired["approve"] + 1))
    ov.deny_clicked.connect(lambda: fired.__setitem__("deny", fired["deny"] + 1))
    ov.open_requested.connect(lambda: fired.__setitem__("open", fired["open"] + 1))
    ov.dismissed.connect(lambda: fired.__setitem__("dismiss", fired["dismiss"] + 1))

    ov.present()
    assert ov._minimized is True, "should rest minimized, not expand on present"
    assert ov._state == "working"

    # Drive every state; handle colour tracks state; content pages switch.
    ov.set_title("Build a site")
    ov.show_working("Reading files…", tool="Read")
    assert ov._stack.currentWidget() is ov._page_working
    assert ov._handle._state == "working"
    assert ov._handle._shows_at_rest(), "working must show at rest — no dead zone while a task runs"
    # Collapsible live "Details" feed: toggling it expands the card and accumulates activity.
    ov._toggle_details()   # isHidden() reflects the explicit setVisible flag; isVisible() needs
    assert ov._details_open and not ov._details_feed.isHidden()   # shown ancestors (false offscreen)
    assert "Reading files" in ov._details_feed.toPlainText(), "Details feed must accumulate actions"
    ov._toggle_details()
    assert not ov._details_open and ov._details_feed.isHidden()
    # Model + effort picker: setting updates the pill label and emits model_changed.
    picked = {}
    ov.model_changed.connect(lambda m, e: picked.update(model=m, effort=e))
    ov.set_model_effort("opus", "high")
    assert "Opus" in ov._model_btn.text() and "High" in ov._model_btn.text(), ov._model_btn.text()
    ov._set_model("sonnet")
    assert picked.get("model") == "sonnet" and "Sonnet" in ov._model_btn.text()
    ov._set_effort("max")
    assert picked.get("effort") == "max" and "Max" in ov._model_btn.text()

    ov.show_needs_you("Bash", {"command": "git push origin main", "timeout": 120000})
    assert ov._stack.currentWidget() is ov._page_needs
    assert ov._state == "needs_you" and ov._handle._state == "needs_you"

    # Hover expands; leaving schedules collapse; pin keeps it open.
    ov.restore_panel()
    assert ov._minimized is False
    ov.minimize_panel()
    assert ov._minimized is True
    ov._on_handle_clicked()
    assert ov._pinned is True and ov._minimized is False

    # Approve resolves via signal and flips back to working.
    ov._on_approve()
    assert fired["approve"] == 1
    assert ov._stack.currentWidget() is ov._page_working

    ov.show_needs_you("Write", {"file_path": "/tmp/x.txt"})
    ov._on_deny()
    assert fired["deny"] == 1

    ov.present()                            # a task starts pinned/up (constantly visible)
    ov.restore_panel()
    ov.show_done("cc7618c9abcd", 0.1234, task="Build a site",
                 result="Subject: Partnership — Here is a short intro email you can send to brands.")
    assert ov._stack.currentWidget() is ov._page_done
    assert not ov._done_timer.isActive(), "done must NOT auto-hide — it stays reopenable"
    assert ov._handle._state == "done"
    assert ov._handle._shows_at_rest(), "done handle must be visible at rest (a click target)"
    assert "intro email" in ov._done_label.text(), "done card must show what Claude GENERATED"
    assert ov._pinned is True, "done stays UP (pinned) — user wants it constantly up, not auto-tucked"
    # The user can still tuck it to a reopenable pill: minimize + click -> expands back to the result.
    ov.minimize_panel()
    assert ov._minimized is True
    ov._on_handle_clicked()
    assert ov._minimized is False and ov._stack.currentWidget() is ov._page_done

    ov.show_error("Couldn't reach the agent.")
    assert ov._stack.currentWidget() is ov._page_error
    assert ov._handle._state == "error"

    # open + dismiss signals.
    ov.open_requested.emit()
    assert fired["open"] == 1
    ov._on_dismiss()
    assert fired["dismiss"] == 1

    # Idempotent / no-raise edge calls.
    ov.minimize_panel()
    ov.minimize_panel()
    ov.show_needs_you("Edit", None)       # None tool_input must not raise
    ov.show_working()                      # empty text/tool must not raise

    # Transcript bubble + primary action (Stop while working / Open in Claude when done).
    ov.present()
    ov.set_title("Make me a landing page for a coffee brand")
    assert "landing page" in ov._transcript.text(), "transcript bubble shows what you said"
    assert "Stop" in ov._primary_btn.text(), "primary is Stop while working"
    stops = []
    ov.stop_clicked.connect(lambda: stops.append(1))
    ov._on_primary()
    assert len(stops) == 1, "Stop fires stop_clicked"
    _o = fired["open"]
    ov.show_done("zz99", 0.0, result="built the landing page")
    assert "Open in Claude" in ov._primary_btn.text(), "primary becomes Open in Claude when done"
    ov._on_primary()
    assert fired["open"] == _o + 1, "primary on done opens Claude"

    print("claude_overlay self-check OK")
