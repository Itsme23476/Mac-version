"""Left-edge conversation rail — the cockpit for concurrent agents.

A VoiceOS-style roster that lives FLUSH against the left screen edge and tucks away to a thin strip
when you're not using it: hover the strip and it slides out; move away and it slides back. One row per
live/recent conversation with a status dot (working / needs-you / done / error) — click to focus it in
the notch. Reuses the same non-activating, all-Spaces, no-focus-steal panel mechanics as the notch
(`notch_panel.configure_macos_panel`), so driving it never steals focus from your current app.
"""
from __future__ import annotations
import time

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QPoint, QEasingCurve
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton

from app.ui import notch_panel

RAIL_TITLE = "Filect Conversations"   # unique — notch_panel finds the NSWindow by this
RAIL_W = 272
EDGE = 7                              # px of the rail left peeking when collapsed (the hover strip)
ANIM_MS = 160

# status -> (dot colour, label). Matches the notch palette.
_STATUS = {
    "working":   ("#D97757", "Working"),
    "needs_you": ("#E6A23C", "Needs you"),
    "done":      ("#6FCF7F", "Done"),
    "error":     ("#F2645A", "Error"),
    "stopped":   ("#6E6E78", "Stopped"),
}


def _elapsed(since: float) -> str:
    s = max(0, int(time.time() - since))
    return f"{s // 60}:{s % 60:02d}"


class _RailRow(QFrame):
    """One conversation: status dot + title + status/elapsed. Clickable to focus it."""
    clicked = Signal()

    def __init__(self, title, status, started_at, focused, parent=None):
        super().__init__(parent)
        self.setObjectName("railRow")
        color, label = _STATUS.get(status, _STATUS["working"])
        sub = f"{label} · {_elapsed(started_at)}" if status == "working" else label
        border = "rgba(217,119,87,0.45)" if focused else "transparent"
        bg = "rgba(217,119,87,0.12)" if focused else "transparent"
        self.setStyleSheet(
            f"QFrame#railRow{{background:{bg};border:1px solid {border};border-radius:10px;}}"
            "QFrame#railRow:hover{background:rgba(255,255,255,0.05);} "
            "QFrame#railRow QLabel{background:transparent;border:none;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(9)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{color};font-size:11px;")
        lay.addWidget(dot, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        t = QLabel((title or "Untitled")[:40])
        t.setStyleSheet("color:#ECECEC;font-size:12.5px;font-weight:600;")
        s = QLabel(sub)
        s.setStyleSheet(f"color:{color if status in ('needs_you', 'error') else '#8A8A92'};font-size:10.5px;")
        col.addWidget(t)
        col.addWidget(s)
        lay.addLayout(col, 1)

    def mousePressEvent(self, e):
        self.clicked.emit()


class ConversationRail(QWidget):
    session_clicked = Signal(str)     # AgentSession.key
    new_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(RAIL_TITLE)
        self.setFixedWidth(RAIL_W)
        self._rows = []
        self._expanded = False
        self._hovered = False
        self._x_open = 0
        self._x_closed = 0
        self._y = 0
        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 10, 10, 10)        # left=0 so the card sits flush on the screen edge
        self._card = QFrame()
        self._card.setObjectName("railCard")
        self._card.setStyleSheet(
            "QFrame#railCard{background:#0E0E11;border:1px solid rgba(255,255,255,0.07);"
            "border-top-left-radius:0px;border-bottom-left-radius:0px;"
            "border-top-right-radius:18px;border-bottom-right-radius:18px;}")
        outer.addWidget(self._card)
        col = QVBoxLayout(self._card)
        col.setContentsMargins(12, 12, 10, 12)
        col.setSpacing(5)
        hdr = QLabel("CHATS")
        hdr.setStyleSheet("color:#7A7A86;font-size:10.5px;font-weight:800;letter-spacing:1px;background:transparent;")
        col.addWidget(hdr)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        self._rows_box.setContentsMargins(0, 2, 0, 2)
        col.addLayout(self._rows_box)
        self._empty = QLabel("No active chats")
        self._empty.setStyleSheet("color:#6E6E78;font-size:11px;background:transparent;padding:6px 2px;")
        col.addWidget(self._empty)
        self._new_btn = QPushButton("＋  New chat")
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#B9B9C2;border:1px dashed rgba(255,255,255,0.14);"
            "border-radius:10px;padding:7px 10px;font-size:12px;text-align:left;}"
            "QPushButton:hover{color:#ECECEC;border-color:rgba(217,119,87,0.5);}")
        self._new_btn.clicked.connect(self.new_requested.emit)
        col.addWidget(self._new_btn)

    def update_sessions(self, sessions, focused_key):
        """Rebuild the roster (most-recently-started first). Hides the rail when there's nothing;
        peeks open briefly when the set changes so a new chat is noticed, then tucks back."""
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        ordered = sorted(sessions, key=lambda s: s.started_at, reverse=True)
        self._empty.setVisible(not ordered)
        for s in ordered:
            row = _RailRow(s.title, s.status, s.started_at, s.key == focused_key)
            row.clicked.connect(lambda k=s.key: self.session_clicked.emit(k))
            self._rows_box.addWidget(row)
            self._rows.append(row)
        if not ordered:
            self.hide()
            return
        first_show = not self.isVisible()
        self._reanchor()
        if first_show:
            self.show()
            self._configure()
        self._peek()                                   # slide open, then auto-tuck if you don't grab it

    # -- collapse / expand (slide flush against the left edge) -----------------
    def _reanchor(self):
        geo = notch_panel.screen_geo()
        if geo is None:
            return
        self.adjustSize()
        h = min(self.height(), int(geo.height() * 0.72))
        self._y = geo.y() + (geo.height() - h) // 2
        self._x_open = geo.x()                          # flush on the screen's left edge
        self._x_closed = geo.x() - (RAIL_W - EDGE)      # tucked left, only EDGE px peeking
        self.move(self._x_open if self._expanded else self._x_closed, self._y)

    def _slide_to(self, x: int):
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(x, self._y))
        self._anim.start()

    def _set_expanded(self, on: bool):
        self._expanded = on
        self._slide_to(self._x_open if on else self._x_closed)

    def _peek(self):
        """Slide open momentarily (e.g. a new chat appeared), then tuck back unless you're hovering."""
        self._set_expanded(True)
        QTimer.singleShot(2600, lambda: self._set_expanded(False) if not self._hovered else None)

    def enterEvent(self, e):
        self._hovered = True
        self._set_expanded(True)

    def leaveEvent(self, e):
        self._hovered = False
        self._set_expanded(False)

    def _configure(self):
        notch_panel.configure_macos_panel(RAIL_TITLE)
        QTimer.singleShot(10, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
        QTimer.singleShot(60, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
