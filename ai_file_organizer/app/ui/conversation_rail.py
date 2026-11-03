"""Left-edge conversation rail — the cockpit for concurrent agents.

A VoiceOS-style roster that lives FLUSH against the left screen edge and tucks away to a thin strip
when you're not using it: hover the strip and it slides out; move away and it slides back. Grouped like
Claude's sidebar — RUNNING (chats working or waiting on you right now) pinned on top, then Today /
Previous 7 days / Previous 30 days / one group per month for everything else (this run's finished
chats + past voice-agent chats from disk), with search. Selecting a conversation shows it in the
notch. Reuses the same non-activating,
all-Spaces, no-focus-steal panel mechanics as the notch (`notch_panel.configure_macos_panel`), so
driving it never steals focus from your current app.
"""
from __future__ import annotations
import sys
import time
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QPoint, QEasingCurve
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
                               QScrollArea)

from app.ui import notch_panel
from app.ui.claude_overlay import _TypeField   # a QLineEdit that runs a callback on click (keys the panel)

RAIL_TITLE = "Filect Conversations"   # unique — notch_panel finds the NSWindow by this
RAIL_W = 272
EDGE = 7                              # px of the rail left peeking when collapsed (the hover strip)
ANIM_MS = 160
_RUNNING = ("working", "needs_you")

# status -> (dot colour, label). Running states are coloured; finished history is neutral so viewing an
# old chat doesn't change how it looks — only errors keep their colour.
_STATUS = {
    "working":   ("#D97757", "Working"),
    "needs_you": ("#E6A23C", "Needs you"),
    "done":      ("#6E6E78", ""),
    "error":     ("#F2645A", "Error"),
    "stopped":   ("#6E6E78", "Stopped"),
    "recent":    ("#6E6E78", ""),           # a past chat from disk
}
_HDR_CSS = "color:#7A7A86;font-size:10.5px;font-weight:800;letter-spacing:1px;background:transparent;"
_SCROLL_CSS = (
    "QScrollArea{background:transparent;border:none;}"
    "QScrollBar:vertical{background:transparent;width:6px;margin:0;}"
    "QScrollBar::handle:vertical{background:rgba(255,255,255,0.14);border-radius:3px;min-height:24px;}"
    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}")


def _elapsed(since: float) -> str:
    s = max(0, int(time.time() - since))
    return f"{s // 60}:{s % 60:02d}"


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 3600:
        return f"{max(1, s // 60)}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 172800:
        return "Yesterday"
    return f"{s // 86400}d ago"


def _group(sessions, recent, now=None):
    """[(header, [(kind, obj), ...]), ...] in display order, like Claude's sidebar. RUNNING = this run's
    chats working or waiting on you; everything else — this run's finished chats ("session") and past
    chats from disk ("recent") — newest first, by the calendar day of last activity: TODAY, PREVIOUS
    7 DAYS, PREVIOUS 30 DAYS, then one group per month ("SEPTEMBER"; "DECEMBER 2025" once it's not
    this year). Empty groups are omitted."""
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now)
    start = today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    running = sorted((s for s in sessions if s.status in _RUNNING), key=lambda s: s.started_at, reverse=True)
    rest = [(s.started_at, "session", s) for s in sessions if s.status not in _RUNNING]
    rest += [(r.mtime, "recent", r) for r in recent]
    rest.sort(key=lambda t: t[0], reverse=True)
    grouped = {}                        # insertion order = newest group first (rest is newest-first)
    for t, kind, obj in rest:
        if t >= start:
            name = "TODAY"
        elif t >= start - 7 * 86400:
            name = "PREVIOUS 7 DAYS"
        elif t >= start - 30 * 86400:
            name = "PREVIOUS 30 DAYS"
        else:
            d = datetime.fromtimestamp(t)
            name = d.strftime("%B").upper() + ("" if d.year == today.year else f" {d.year}")
        grouped.setdefault(name, []).append((kind, obj))
    out = [("RUNNING", [("session", s) for s in running])] if running else []
    return out + list(grouped.items())


def _matches(query: str, *fields) -> bool:
    """Every word of the query appears somewhere in the fields (case-insensitive)."""
    hay = " ".join(f or "" for f in fields).lower()
    return all(w in hay for w in query.lower().split())


class _RailRow(QFrame):
    """One conversation: status dot + title + status/elapsed or how long ago. Clickable."""
    clicked = Signal()

    def __init__(self, title, status, when, focused, parent=None):
        super().__init__(parent)
        self.setObjectName("railRow")
        color, label = _STATUS.get(status, _STATUS["working"])
        if status == "working":
            sub = f"{label} · {_elapsed(when)}"
        elif status == "needs_you":
            sub = label
        elif label:                              # error / stopped: say so, plus when
            sub = f"{label} · {_ago(when)}"
        else:                                    # finished / past: just when
            sub = _ago(when)
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
    session_clicked = Signal(str)     # AgentSession.key — a chat from this run
    new_requested = Signal()
    recent_clicked = Signal(str)      # claude session id — a past chat from disk

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pinned = False          # a click pins it open so it stays while you use the notch
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(RAIL_TITLE)
        self.setFixedWidth(RAIL_W)
        self._rows = []               # every row widget, in display order
        self._groups = []             # [(header, row count)] as last rendered
        self._data = ([], None, [])   # (sessions, focused_key, recent) from the last update_sessions
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
        col.setSpacing(8)
        # Search: filters the list as you type. Clicking it pins the rail and keys the panel so it can
        # take typing (see _focus_search).
        self._search = _TypeField(self._focus_search)
        self._search.setObjectName("railSearch")
        self._search.setPlaceholderText("Search chats")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            "QLineEdit#railSearch{background:#17171C;color:#ECECEC;border:1px solid rgba(255,255,255,0.08);"
            "border-radius:10px;padding:6px 10px;font-size:12px;}"
            "QLineEdit#railSearch:focus{border-color:rgba(217,119,87,0.6);}")
        self._search.textChanged.connect(lambda _t: self._render())
        col.addWidget(self._search)
        self._new_btn = QPushButton("＋  New chat")
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#B9B9C2;border:1px dashed rgba(255,255,255,0.14);"
            "border-radius:10px;padding:7px 10px;font-size:12px;text-align:left;}"
            "QPushButton:hover{color:#ECECEC;border-color:rgba(217,119,87,0.5);}")
        self._new_btn.clicked.connect(self.new_requested.emit)
        col.addWidget(self._new_btn)
        # The grouped list scrolls. Every layer is explicitly transparent: an unstyled viewport falls
        # back to the app's LIGHT palette and paints a white box on the dark card.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_SCROLL_CSS)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setStyleSheet("background:transparent;")
        self._list = QWidget()
        self._list.setObjectName("railList")
        self._list.setStyleSheet("QWidget#railList{background:transparent;}")
        self._list_box = QVBoxLayout(self._list)
        self._list_box.setContentsMargins(0, 0, 4, 0)
        self._list_box.setSpacing(4)
        self._scroll.setWidget(self._list)
        col.addWidget(self._scroll)
        col.addStretch(1)                  # spare height goes BELOW the list — never float items mid-rail

    def update_sessions(self, sessions, focused_key, recent=()):
        """Rebuild the grouped roster (see _group), keeping any search filter. Peeks open briefly while
        chats are running so a new one is noticed, then tucks back; otherwise stays the collapsed strip,
        always summonable."""
        self._data = (list(sessions), focused_key, list(recent))
        first_show = not self.isVisible()
        self._render()
        if first_show:
            self.show()
            self._configure()
        if self._groups and self._groups[0][0] == "RUNNING":
            self._peek()            # something's running → slide open briefly, then auto-tuck

    def _render(self):
        """Rebuild the rows from the last data + the search text, then resize the rail to fit."""
        sessions, focused_key, recent = self._data
        q = self._search.text().strip()
        if q:
            sessions = [s for s in sessions if _matches(q, s.title, getattr(s, "prompt", ""))]
            recent = [r for r in recent if _matches(q, r.title, getattr(r, "preview", ""))]
        while self._list_box.count():
            item = self._list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows = []
        # ponytail: rebuilds every row on each refresh (called per agent step) — fine at today's ~50-300
        # chats; update rows in place if history grows into the thousands.
        groups = _group(sessions, recent)
        added = []
        for i, (header, items) in enumerate(groups):
            h = QLabel(header)
            h.setStyleSheet(_HDR_CSS + ("padding-top:8px;" if i else ""))
            added.append(h)
            for kind, obj in items:
                if kind == "session":
                    row = _RailRow(obj.title, obj.status, obj.started_at, obj.key == focused_key)
                    row.clicked.connect(lambda k=obj.key: self.session_clicked.emit(k))
                else:
                    row = _RailRow(obj.title, "recent", obj.mtime, False)
                    row.clicked.connect(lambda sid=obj.id: self.recent_clicked.emit(sid))
                added.append(row)
                self._rows.append(row)
        if not groups:
            empty = QLabel("No matches" if q else "No chats yet")
            empty.setStyleSheet("color:#6E6E78;font-size:11px;background:transparent;padding:6px 2px;")
            added.append(empty)
        for w in added:
            self._list_box.addWidget(w)
            # Show NOW: on an already-visible rail Qt only shows new children on a QUEUED event, and
            # until then they measure as hidden (0 height) — which sized the list to 40px on every
            # rebuild (the "only one chat left" bug after selecting a conversation).
            w.show()
        self._list_box.addStretch(1)
        self._groups = [(h, len(items)) for h, items in groups]
        self._reanchor()

    def _focus_search(self):
        """You clicked the search box: pin the rail open and let the non-activating panel take keyboard
        focus. A USER click may key a floating becomesKeyOnlyIfNeeded panel — same as the notch's type
        field."""
        self.open_full()
        if sys.platform != "darwin":
            return
        try:
            ns = notch_panel._ns_window(RAIL_TITLE)
            if ns is not None and hasattr(ns, "makeKeyWindow"):
                ns.makeKeyWindow()
        except Exception:
            pass

    # -- collapse / expand (slide flush against the left edge) -----------------
    def _reanchor(self):
        geo = notch_panel.screen_geo()
        if geo is None:
            return
        # Size the list to its content, capped so the whole rail fits in 72% of the screen (it scrolls).
        col = self._card.layout()
        chrome = (10 + 10 + 12 + 12 + 2 * col.spacing()
                  + self._search.sizeHint().height() + self._new_btn.sizeHint().height())
        cap = int(geo.height() * 0.72) - chrome
        self._scroll.setFixedHeight(max(40, min(self._list.sizeHint().height(), cap)))
        # The outer layout caches the card's size hint and only refreshes it when the CARD reports a
        # change (not when its inner scroll area does) — without this the rail lagged one resize behind.
        self._card.updateGeometry()
        self.layout().activate()
        self.resize(RAIL_W, self.sizeHint().height())   # explicit: adjustSize() never SHRANK the rail
        # Anchor the TOP where a full-height rail would start, so the rail grows/shrinks downward and
        # the search box doesn't jump out from under you as results change.
        self._y = geo.y() + (geo.height() - (cap + chrome)) // 2
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
        if not self._pinned:          # a click pins it open; otherwise tuck back when you leave
            self._set_expanded(False)

    def mousePressEvent(self, e):
        # A click on the rail body (not a row — rows consume their own click) TOGGLES it: opens+pins,
        # or closes if it's already open. Independent of the notch — clicking the rail never opens it.
        if self._pinned:
            self.collapse()
        else:
            self.open_full()
        super().mousePressEvent(e)

    def open_full(self):
        """Pin the rail open (its own click action — the notch is not affected)."""
        self._pinned = True
        self._set_expanded(True)

    def collapse(self):
        self._pinned = False
        self._set_expanded(False)

    def _configure(self):
        notch_panel.configure_macos_panel(RAIL_TITLE)
        QTimer.singleShot(10, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
        QTimer.singleShot(60, lambda: notch_panel.configure_macos_panel(RAIL_TITLE))
