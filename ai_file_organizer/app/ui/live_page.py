"""The Assistant tab — Filect Assistant, hands-free (phase 1).

Start → Filect listens (app/core/live_listener.py). Each thing you say, once you've stopped talking for
"send after" seconds, runs exactly like an fn + ⌃ request: AssistantPage.run_voice_task(text,
source="live") → router (which may IGNORE what isn't a request) → Claude / Codex → the notch card. This
page shows what it's doing (status, mic level, an activity list of what it heard and what happened),
and its settings. Stop (here, or in the menu-bar menu) ends it; it never starts by itself."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QAction
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

logger = logging.getLogger("ai_file_organizer")

ACCENT = "#7C4DFF"
AMBER = "#E0A33A"
RED = "#E5534B"
GREEN = "#3FB97F"
MAX_ROWS = 100
SAY_EXAMPLE = "“make a landing page for my coffee shop”"

# listener state → the notch light
_NOTCH = {"starting": "listening", "listening": "listening", "countdown": "listening",
          "hearing": "hearing", "paused": "paused"}


def _track(event: str, **props) -> None:
    try:
        from app.core.supabase_client import track
        track(event, **props)
    except Exception:
        pass


def _chime(name: str = "Tink") -> None:
    try:
        import AppKit
        snd = AppKit.NSSound.soundNamed_(name)
        if snd is not None:
            snd.play()
    except Exception:
        pass


class _Meter(QWidget):
    """A thin mic-level bar (so you can see it hears you)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._v = 0.0
        self._on = False
        self._track = "#252535"

    def set_level(self, v: float) -> None:
        v = max(0.0, min(1.0, v))
        self._v = v if v > self._v else self._v * 0.75 + v * 0.25     # fast attack, smooth release
        self.update()

    def set_on(self, on: bool) -> None:
        self._on = on
        if not on:
            self._v = 0.0
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._track))
        p.drawRoundedRect(self.rect(), 3, 3)
        if self._on and self._v > 0.01:
            w = max(6, int(self.width() * self._v))
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(0, 0, w, self.height(), 3, 3)
        p.end()


class _Row(QFrame):
    """One thing it heard: time · what you said · what happened. Clicking opens that chat."""

    def __init__(self, page, uid):
        super().__init__()
        self._page, self.uid, self.key = page, uid, ""
        self.setObjectName("liveRow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.when = QLabel(time.strftime("%H:%M"))
        self.outcome = QLabel("Transcribing…")
        top.addWidget(self.when, 0)
        top.addWidget(self.outcome, 1)
        lay.addLayout(top)
        self.text = QLabel("…")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.text)
        self.restyle()

    def set_text(self, text: str) -> None:
        self.text.setText(f"“{text}”" if text else "…")

    def set_outcome(self, text: str, color: str, key: str = "") -> None:
        self.outcome.setText(text)
        self.outcome.setProperty("color", color)
        self.key = key or ""
        self.setCursor(Qt.PointingHandCursor if self.key else Qt.ArrowCursor)
        self.setToolTip("Open this chat" if self.key else "")
        self.restyle()

    def restyle(self):
        c = self._page._c
        self.setStyleSheet(f"QFrame#liveRow {{ background:{c['input_bg']}; border:1px solid {c['border']}; "
                           "border-radius:10px; } QLabel { background:transparent; border:none; }")
        self.when.setStyleSheet(f"color:{c['text_muted']}; font-size:11.5px;")
        col = self.outcome.property("color") or c["text_muted"]
        self.outcome.setStyleSheet(f"color:{col}; font-size:12px; font-weight:600;")
        self.text.setStyleSheet(f"color:{c['text']}; font-size:13px;")

    def mousePressEvent(self, e):
        if self.key:
            self._page._open_chat(self.key)
        super().mousePressEvent(e)


class LivePage(QScrollArea):
    def __init__(self, main_window, listener=None):
        super().__init__()
        self._mw = main_window
        self._c = get_theme_colors()
        self.setObjectName("livePage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)
        if listener is None:
            from app.core.live_listener import LiveListener
            listener = LiveListener(self)
        self.listener = listener
        self._rows = {}                 # uid → _Row
        self._started_at = None
        self._state = "off"
        self._tray_action = None
        self._build()
        L = self.listener
        L.state_changed.connect(self._on_state)
        L.level.connect(self._meter.set_level)
        L.countdown.connect(self._on_countdown)
        L.utterance_ended.connect(self._on_ended)
        L.heard.connect(self._on_heard)
        L.dropped.connect(self._on_dropped)
        L.usage.connect(self._on_usage)
        L.esc_late.connect(self._on_esc_late)
        self._on_usage(L.used_today())
        self._noted = set()             # uids whose "Cancelled" note was already shown
        self._hooked_ap = None
        QTimer.singleShot(0, self._add_tray_item)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.listener.stop)

    # ---- layout ------------------------------------------------------------------------------------
    def _build(self):
        page = QWidget()
        page.setObjectName("liveContent")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(40, 30, 40, 30)
        lay.setSpacing(16)
        from app.ui.icons import line_pixmap
        trow = QHBoxLayout()
        trow.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(line_pixmap("waveform", 22, ACCENT))
        ic.setStyleSheet("background: transparent; border: none;")
        trow.addWidget(ic, 0, Qt.AlignVCenter)
        self._title = QLabel("Assistant")
        self._title.setStyleSheet("font-family: 'Sora', 'SF Pro Display', sans-serif; font-size: 22px; "
                                  f"font-weight: 700; color: {ACCENT}; background: transparent; border: none;")
        trow.addWidget(self._title, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        lay.addLayout(trow)
        self._sub = QLabel("Hands-free: click Start and just talk. Each thing you ask runs like fn + ⌃ — "
                           "Claude Code or Codex does it, and the card at the notch shows it. It listens "
                           "until you click Stop.")
        self._sub.setWordWrap(True)
        lay.addWidget(self._sub)

        # -- the main card: Start / Stop, status, level ---------------------------------------------
        self._hero = QFrame()
        self._hero.setObjectName("settingsCard")
        h = QVBoxLayout(self._hero)
        h.setContentsMargins(24, 22, 24, 22)
        h.setSpacing(12)
        brow = QHBoxLayout()
        self.start_btn = QPushButton("Start Filect Assistant")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setMinimumHeight(46)
        self.start_btn.setMinimumWidth(260)
        self.start_btn.clicked.connect(self.toggle)
        brow.addWidget(self.start_btn, 0)
        brow.addStretch(1)
        h.addLayout(brow)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self._dot = QLabel("●")
        self.status = QLabel("")
        srow.addWidget(self._dot, 0)
        srow.addWidget(self.status, 1)
        h.addLayout(srow)
        self._meter = _Meter()
        h.addWidget(self._meter)
        self._hint = QLabel("Only your speech is sent to be transcribed — never the silence — and that's "
                            "what counts toward your daily voice limit. It steps aside while you use a "
                            "Filect shortcut, while another app uses the mic, and while your Mac is locked. "
                            "Esc throws away what you're saying.")
        self._hint.setWordWrap(True)
        h.addWidget(self._hint)
        self.usage_label = QLabel("")
        h.addWidget(self.usage_label)
        lay.addWidget(self._hero)

        # -- settings -----------------------------------------------------------------------------------
        self._set_card = QFrame()
        self._set_card.setObjectName("settingsCard")
        sc = QVBoxLayout(self._set_card)
        sc.setContentsMargins(24, 20, 24, 20)
        sc.setSpacing(14)
        self._set_title = QLabel("Settings")
        sc.addWidget(self._set_title)

        self._wait_lbl = QLabel("Send after I stop talking for")
        self._wait_val = QLabel("")
        r = QHBoxLayout()
        r.addWidget(self._wait_lbl, 1)
        r.addWidget(self._wait_val, 0)
        sc.addLayout(r)
        self.wait_slider = QSlider(Qt.Horizontal)
        self.wait_slider.setRange(2, 20)                       # half-seconds: 1.0 – 10.0 s
        self.wait_slider.setSingleStep(1)
        self.wait_slider.setPageStep(2)
        self.wait_slider.setValue(int(round(float(settings.live_send_after_sec) * 2)))
        self.wait_slider.valueChanged.connect(self._on_wait)
        sc.addWidget(self.wait_slider)
        self._on_wait(self.wait_slider.value(), save=False)

        self._sens_lbl = QLabel("Picks up")
        sc.addWidget(self._sens_lbl)
        srow2 = QHBoxLayout()
        srow2.setSpacing(8)
        self.sens_group = QButtonGroup(self)
        self.sens_group.setExclusive(True)
        self.sens_btns = {}
        for key, label in (("low", "Only close voices"), ("medium", "Normal"), ("high", "Softer voices too")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(32)
            b.setChecked(settings.live_sensitivity == key)
            b.clicked.connect(lambda _=False, k=key: self._on_sens(k))
            self.sens_group.addButton(b)
            self.sens_btns[key] = b
            srow2.addWidget(b)
        srow2.addStretch(1)
        sc.addLayout(srow2)
        self._sens_hint = QLabel("If it misses you, pick “Softer voices too”; if it picks up people around "
                                 "you, pick “Only close voices”.")
        self._sens_hint.setWordWrap(True)
        sc.addWidget(self._sens_hint)

        self.noise_toggle, self._noise_lbl, self._noise_hint = self._toggle_row(
            sc, "Filter background noise", settings.live_noise_filter, self._on_noise,
            "Apple's voice isolation: removes background noise and whatever your Mac is playing. Uses a "
            "little more battery.")
        self.chime_toggle, self._chime_lbl, _ = self._toggle_row(
            sc, "Chime when a request starts", settings.live_chime, self._on_chime, "")
        self.speak_toggle, self._speak_lbl, self._speak_hint = self._toggle_row(
            sc, "Speak replies", False, lambda on: None, "Coming soon.")
        self.speak_toggle.setEnabled(False)
        lay.addWidget(self._set_card)

        # -- activity -----------------------------------------------------------------------------------
        self._act_card = QFrame()
        self._act_card.setObjectName("settingsCard")
        ac = QVBoxLayout(self._act_card)
        ac.setContentsMargins(24, 20, 24, 20)
        ac.setSpacing(10)
        arow = QHBoxLayout()
        self._act_title = QLabel("Activity")
        arow.addWidget(self._act_title, 1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_activity)
        arow.addWidget(self.clear_btn, 0)
        ac.addLayout(arow)
        self._empty = QLabel(f"Nothing yet — click Start and say something like {SAY_EXAMPLE}.")
        self._empty.setWordWrap(True)
        ac.addWidget(self._empty)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(8)
        ac.addLayout(self._rows_box)
        lay.addWidget(self._act_card)
        lay.addStretch(1)
        self.setWidget(page)
        self.apply_theme()
        self._on_state("off", "")

    def _toggle_row(self, parent_layout, label, on, handler, hint):
        row = QHBoxLayout()
        lbl = QLabel(label)
        row.addWidget(lbl, 1)
        t = QPushButton()
        t.setCheckable(True)
        t.setCursor(Qt.PointingHandCursor)
        t.setFixedSize(66, 30)
        t.setChecked(bool(on))
        t.toggled.connect(lambda v, b=t: (self._style_toggle(b), handler(v)))
        row.addWidget(t, 0)
        parent_layout.addLayout(row)
        h = None
        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            parent_layout.addWidget(h)
        self._style_toggle(t)
        return t, lbl, h

    def _style_toggle(self, t):
        on = t.isChecked()
        t.setText("On" if on else "Off")
        bg = ACCENT if on else "#3A3A48"          # (same On/Off look as the Voice tab's toggles)
        c = getattr(self, "_c", None) or get_theme_colors()
        t.setStyleSheet(f"QPushButton {{ background-color:{bg}; color:white; border:none; border-radius:15px; "
                        "font-size:12px; font-weight:600; } "
                        f"QPushButton:disabled {{ background-color:{c['border_strong']}; color:{c['text_disabled']}; }}")

    def apply_theme(self, theme=None):
        self._c = c = get_theme_colors(theme)
        self.setStyleSheet(f"QScrollArea#livePage, QWidget#liveContent {{ background:{c['bg']}; border:none; }}")
        card = (f"QFrame#settingsCard {{ background-color:{c['surface']}; border:1px solid {c['border']}; "
                "border-radius:16px; } QFrame#settingsCard QLabel { border:none; background:transparent; }")
        for w in (self._hero, self._set_card, self._act_card):
            w.setStyleSheet(card)
        muted = f"color:{c['text_muted']}; font-size:12.5px; background:transparent; border:none;"
        self._sub.setStyleSheet(f"color:{c['text_muted']}; font-size:13px; background:transparent; border:none;")
        for w in (self._hint, self._sens_hint, self._noise_hint, self._speak_hint, self._empty, self.usage_label):
            if w is not None:
                w.setStyleSheet(muted)
        head = (f"font-family:'Sora','SF Pro Display',sans-serif; font-size:15px; font-weight:600; "
                f"color:{ACCENT}; background:transparent; border:none;")
        self._set_title.setStyleSheet(head)
        self._act_title.setStyleSheet(head)
        body = f"color:{c['text']}; font-size:13.5px; background:transparent; border:none;"
        for w in (self._wait_lbl, self._sens_lbl, self._noise_lbl, self._chime_lbl, self._speak_lbl):
            w.setStyleSheet(body)
        self._wait_val.setStyleSheet(f"color:{ACCENT}; font-size:13.5px; font-weight:600; background:transparent;")
        self.wait_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height:4px; background:{c['border_strong']}; border-radius:2px; }}"
            f"QSlider::sub-page:horizontal {{ background:{ACCENT}; border-radius:2px; }}"
            f"QSlider::handle:horizontal {{ background:{ACCENT}; width:16px; height:16px; margin:-6px 0; "
            "border-radius:8px; }")
        seg = (f"QPushButton {{ background:{c['input_bg']}; color:{c['text_secondary']}; border:1px solid "
               f"{c['border']}; border-radius:9px; padding:0 14px; font-size:12.5px; }}"
               f"QPushButton:checked {{ background:{c['purple_light_bg']}; color:{ACCENT}; border-color:{ACCENT}; "
               "font-weight:600; }")
        for b in self.sens_btns.values():
            b.setStyleSheet(seg)
        self.clear_btn.setStyleSheet(f"QPushButton {{ background:transparent; color:{c['text_muted']}; border:none; "
                                     f"font-size:12px; }} QPushButton:hover {{ color:{c['text']}; }}")
        for t in (self.noise_toggle, self.chime_toggle, self.speak_toggle):
            self._style_toggle(t)
        self._meter._track = c["border_strong"]
        self._meter.update()
        for row in self._rows.values():
            row.restyle()
        self._style_start()
        self._on_state(self._state, getattr(self, "_detail", ""))

    def _style_start(self):
        c = self._c
        if self.listener.on:
            self.start_btn.setText("Stop")
            self.start_btn.setStyleSheet(
                f"QPushButton {{ background:{c['danger_bg']}; color:{c['danger_text']}; border:1px solid "
                f"{c['danger_border']}; border-radius:12px; font-size:15px; font-weight:700; padding:0 26px; }}"
                f"QPushButton:hover {{ background:{c['danger_hover']}; }}")
        else:
            self.start_btn.setText("Start Filect Assistant")
            self.start_btn.setStyleSheet(
                f"QPushButton {{ background:{ACCENT}; color:white; border:none; border-radius:12px; "
                "font-size:15px; font-weight:700; padding:0 26px; } QPushButton:hover { background:#8E63FF; }")

    # ---- start / stop --------------------------------------------------------------------------------
    def toggle(self):
        if self.listener.on:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.listener.on:
            return
        ap = getattr(self._mw, "assistant_page", None)
        if ap is not None and self._hooked_ap is not ap:
            ap.live_routed.connect(self._on_routed)
            self._hooked_ap = ap
        if ap is not None and hasattr(ap, "live_answer_context") and hasattr(self.listener, "set_answer_probe"):
            # a short "yes" counts while the agent is waiting for your answer (else: noise, never sent)
            self.listener.set_answer_probe(lambda: ap.live_answer_context() is not None)
        self._started_at = time.time()
        self.listener.start()
        self._style_start()
        _track("live_started", send_after=float(settings.live_send_after_sec),
               sensitivity=settings.live_sensitivity, noise_filter=bool(settings.live_noise_filter))

    def stop(self):
        if not self.listener.on:
            self._style_start()
            return
        self.listener.stop()
        self._style_start()
        if self._started_at:
            _track("live_stopped", minutes=round((time.time() - self._started_at) / 60.0, 1))
            self._started_at = None

    # ---- listener → UI ---------------------------------------------------------------------------------
    def _on_state(self, state: str, detail: str):
        self._state, self._detail = state, detail
        c = self._c
        text, color = {
            "off": ("Off — click Start and just talk.", c["text_muted"]),
            "starting": (detail or "Starting the microphone…", c["text_muted"]),
            "listening": ("Listening — just talk", ACCENT),
            "hearing": ("Hearing you…", ACCENT),
            "countdown": ("Sending soon · Esc cancels", ACCENT),
            "paused": (detail or "Paused", AMBER),
            "error": (detail or "Something went wrong.", RED),
        }.get(state, (detail or state, c["text_muted"]))
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color}; font-size:14px; font-weight:600; background:transparent;")
        self._dot.setStyleSheet(f"color:{color}; font-size:12px; background:transparent;")
        self._meter.set_on(state in ("listening", "hearing", "countdown"))
        self._style_start()
        self._notch_light(_NOTCH.get(state, ""))
        if self._tray_action is not None:
            self._tray_action.setText("Stop Filect Assistant" if self.listener.on else "Start Filect Assistant")
        if state == "error":
            _track("live_error", kind=(detail or "")[:40])

    def _on_countdown(self, left: float):
        if self._state == "countdown":
            self.status.setText(f"Sending in {left:.1f} s · Esc cancels")

    def _on_usage(self, secs: float):
        mins = secs / 60.0
        used = f"{mins:.0f} min" if mins >= 1 else f"{secs:.0f} s"
        self.usage_label.setText(f"Voice sent by Filect Assistant today: ≈ {used}  ·  part of your 3-hour "
                                 "daily voice limit")

    def _row(self, uid) -> "_Row":
        row = self._rows.get(uid)
        if row is None:
            row = _Row(self, uid)
            self._rows[uid] = row
            self._rows_box.insertWidget(0, row)
            self._empty.setVisible(False)
            while len(self._rows) > MAX_ROWS:
                old = min(self._rows)
                w = self._rows.pop(old)
                w.setParent(None)
                w.deleteLater()
        return row

    def _on_ended(self, uid: int):
        self._row(uid).set_outcome("Transcribing…", self._c["text_muted"])

    def _on_heard(self, uid: int, text: str):
        row = self._row(uid)
        row.set_text(text)
        row.set_outcome("Checking…", self._c["text_muted"])
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            row.set_outcome("The agents aren't ready yet", AMBER)
            return
        if self._hooked_ap is not ap:
            ap.live_routed.connect(self._on_routed)
            self._hooked_ap = ap
        ap.run_voice_task(text, source="live", tag=uid)

    def _on_routed(self, tag, outcome: str, title: str, key: str = ""):
        row = self._rows.get(tag)
        if outcome in ("new", "continued"):
            self.listener.learn(tag)
            if settings.live_chime:
                _chime()
            if row is not None:
                verb = "Started" if outcome == "new" else "Continued"
                row.set_outcome(f"{verb} “{title}”  →", ACCENT, key)
            _track("live_task", outcome=outcome)
        elif outcome in ("approved", "denied"):        # "yes" / "no" to a waiting approval
            self.listener.learn(tag)
            if settings.live_chime and outcome == "approved":
                _chime()
            if row is not None:
                row.set_outcome(f"{'Approved' if outcome == 'approved' else 'Denied'} — “{title}”  →",
                                ACCENT if outcome == "approved" else AMBER, key)
            self._note("✓  Approved" if outcome == "approved" else "✕  Denied", "#FFFFFF")
            _track("live_task", outcome=outcome)
        elif outcome == "ignored":
            if row is not None:
                row.set_outcome("Ignored — not a request", self._c["text_muted"])
            self._note("Not a request")
            _track("live_ignored")
        elif outcome == "cancelled":                 # Esc while the router was still checking it
            if row is not None:
                row.set_outcome("Cancelled (Esc) — not sent", self._c["text_muted"])
            self._cancel_feedback(tag)
        else:
            if row is not None:
                row.set_outcome("Couldn't check this one — say it again, or check Claude Code / Codex in Agents",
                                AMBER)
            self._note("Couldn't check that", AMBER)
            _track("live_error", kind="route_failed")

    def _on_dropped(self, uid: int, text: str, why: str):
        row = self._rows.get(uid)
        if row is None and why not in ("cancelled",) and not text:
            return                          # a cough / a paused half-sentence: not worth a row
        row = self._row(uid)
        if text:
            row.set_text(text)
        label = {"cancelled": "Cancelled (Esc) — not sent",
                 "stopped": "Not sent — you clicked Stop"}.get(why, f"Not sent — {why}")
        row.set_outcome(label, self._c["text_muted"])
        if why == "cancelled":
            self._cancel_feedback(uid)

    def _on_esc_late(self):
        """Esc after it stopped listening to that request: cancel the newest one still being checked."""
        ap = getattr(self._mw, "assistant_page", None)
        checking = [u for u, r in self._rows.items() if r.outcome.text() == "Checking…"]
        if ap is not None and checking and hasattr(ap, "cancel_live"):
            ap.cancel_live(max(checking))

    def _cancel_feedback(self, uid):
        """Make a cancel unmistakable where you're looking: a note under the notch (+ a short sound)."""
        if uid in self._noted:
            return
        self._noted.add(uid)
        self._note("✕  Cancelled", "#FFFFFF")
        if settings.live_chime:
            _chime("Pop")
        _track("live_cancelled")

    def _note(self, text: str, color: str = "#B8B8C4"):
        ap = getattr(self._mw, "assistant_page", None)
        try:
            ov = ap._ensure_notch() if ap is not None else None
            if ov is not None and hasattr(ov, "flash_note"):
                ov.flash_note(text, color)
        except Exception:
            pass

    def clear_activity(self):
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._empty.setVisible(True)

    def _open_chat(self, key: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            return
        try:
            self._mw._on_nav_clicked(5)
            btn = self._mw.nav_button_group.button(5)
            if btn is not None:
                btn.setChecked(True)
            if key in getattr(ap, "_sessions", {}):
                ap._focus(key)
        except Exception as e:
            logger.info("[live] open chat failed: %s", e)

    # ---- settings -------------------------------------------------------------------------------------
    def _save(self):
        try:
            settings._save_config()
        except Exception as e:
            logger.warning("[live] couldn't save settings: %s", e)
        self.listener.apply_settings()

    def _on_wait(self, v: int, save: bool = True):
        secs = v / 2.0
        self._wait_val.setText(f"{secs:.1f} s")
        if save:
            settings.live_send_after_sec = secs
            self._save()

    def _on_sens(self, key: str):
        settings.live_sensitivity = key
        self._save()

    def _on_noise(self, on: bool):
        settings.live_noise_filter = bool(on)
        self._save()

    def _on_chime(self, on: bool):
        settings.live_chime = bool(on)
        self._save()

    # ---- notch light + menu bar ---------------------------------------------------------------------------
    def _notch_light(self, mode: str):
        ap = getattr(self._mw, "assistant_page", None)
        if ap is None:
            return
        try:
            ov = ap._ensure_notch() if mode else getattr(ap, "_overlay", None)
            if ov is not None:
                ov.set_listening(mode)
        except Exception:
            pass

    def _add_tray_item(self):
        try:
            from app.ui.assistant_page import _get_or_create_tray
            tray = _get_or_create_tray()
            menu = getattr(tray, "_menu", None) if tray is not None else None
            if menu is None or self._tray_action is not None:
                return
            act = QAction("Start Filect Assistant", menu)
            act.triggered.connect(self.toggle)
            first = menu.actions()[0] if menu.actions() else None
            menu.insertAction(first, act)
            menu.insertSeparator(first)
            self._tray_action = act
        except Exception as e:
            logger.info("[live] menu-bar item unavailable: %s", e)
