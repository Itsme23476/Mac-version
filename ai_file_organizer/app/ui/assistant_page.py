"""Filect "Assistant" page.

Type a task, hit Run, and a local Claude agent carries it out — you watch each step stream in
live, and whenever it wants to do something risky (write a file, run a command) a card asks you
to Approve or Deny. Each task is a real, saved Claude session (resumable, and visible in Claude
Desktop). The agent work runs on a worker thread; this file bridges it to the Qt UI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton, QFrame, QScrollArea, QCheckBox,
    QLineEdit, QListWidget, QListWidgetItem,
)

from app.ui.theme_manager import theme_manager

# Where the assistant works by default. A dedicated folder keeps it out of the way and makes the
# saved sessions easy to find. (Letting the user pick the folder / resume a specific session is the
# next increment.)
DEFAULT_WORKSPACE = str(Path.home() / "Filect Assistant")

# ── verification tracing ─────────────────────────────────────────────────────────────────────────
# Logs every routing decision + session transition so a test run is fully reconstructable from the
# log. Verify-only: set FILECT_ASSISTANT_TRACE=0 to silence (or strip these before shipping). Tagged
# "[asst]" so it's greppable in ~/.config/ai-file-organizer/logs/ai_file_organizer.log.
import logging as _logging
import os as _os
_log = _logging.getLogger("ai_file_organizer")
_TRACE = _os.environ.get("FILECT_ASSISTANT_TRACE", "1") != "0"


def _trace(msg: str) -> None:
    if _TRACE:
        _log.info("[asst] %s", msg)


# ── "it's done" cues: a chime always, a native notification when you've looked away ──────────────
# A task runs in the background with the notch tucked; these make a finished task reach you even in
# another app. The banner is posted via a system-tray item so it carries the Filect logo — macOS and
# Windows both attribute a notification's icon to the posting app's bundle, and osascript can't.
# The QSystemTrayIcon path ports to Windows (native toast) unchanged. ponytail: stdlib + Qt, no dep.
def _play_chime(ok: bool = True) -> None:
    """Cross-platform so it ports as-is: macOS system sound, Windows MessageBeep, else Qt beep."""
    import sys
    try:
        if sys.platform == "darwin":
            import subprocess
            snd = "/System/Library/Sounds/Glass.aiff" if ok else "/System/Library/Sounds/Basso.aiff"
            subprocess.Popen(["afplay", snd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            import winsound                                   # Windows stdlib
            winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        else:
            from PySide6.QtWidgets import QApplication
            QApplication.beep()
    except Exception:
        pass


_ICON = None          # Filect logo as a QIcon, loaded once
_TRAY = None          # one persistent menu-bar item; kept in a global so Qt doesn't GC it


def _filect_icon():
    """The Filect logo (resources/logo.png) as a QIcon. Same resource lookup the main window uses."""
    global _ICON
    if _ICON is None:
        import sys
        from pathlib import Path
        from PySide6.QtGui import QIcon
        base = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[2]
        p = base / "resources" / "logo.png"
        _ICON = QIcon(str(p)) if p.exists() else QIcon()
    return _ICON


def _get_or_create_tray():
    """One persistent system-tray item carrying the Filect logo. It's the native channel for a
    BRANDED notification (macOS/Windows both take the banner's icon from the posting app), and it
    must be kept in a module global or Qt garbage-collects it and the notifications stop firing."""
    global _TRAY
    try:
        from PySide6.QtWidgets import QSystemTrayIcon, QApplication
        if QApplication.instance() is None:
            return None
        if _TRAY is None:
            _TRAY = QSystemTrayIcon(_filect_icon())
            _TRAY.setToolTip("Filect")
            _TRAY.setVisible(True)
    except Exception:
        return None
    return _TRAY


def _notify(title: str, body: str) -> None:
    """Native, Filect-branded 'done' banner. The tray posts it with the Filect logo — in the shipped
    app and on Windows that's the whole story. A bare `python main.py` dev run has no app bundle, so
    the tray banner silently won't render; there we also fire osascript so dev still gets a cue
    (generic icon — the logo appears once bundled). ponytail: one tray call, no new dependency."""
    import sys
    title = (" ".join((title or "").split()) or "Filect")[:60]
    body = " ".join((body or "").split())[:140]
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        tray = _get_or_create_tray()
        if tray is not None and QSystemTrayIcon.supportsMessages():
            tray.showMessage(title, body, _filect_icon(), 6000)
    except Exception:
        pass
    if sys.platform == "darwin" and not getattr(sys, "frozen", False):   # dev fallback (see above)
        import subprocess
        t = title.replace("\\", "").replace('"', "'")
        b = body.replace("\\", "").replace('"', "'")
        try:
            subprocess.Popen(["osascript", "-e", f'display notification "{b}" with title "{t}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _app_is_active() -> bool:
    """True when Filect is the frontmost app — then the chime is enough and we skip the notification."""
    try:
        from PySide6.QtWidgets import QApplication
        return QApplication.instance().applicationState() == Qt.ApplicationActive
    except Exception:
        return True


class AgentWorker(QObject):
    """Runs one AgentRunner task inside an asyncio loop on a worker thread, and relays everything to
    the UI via Qt signals. Approvals are the tricky bit: the agent's async permission callback emits
    `approvalRequested` and then *awaits* a future; the UI resolves it (from the main thread) by
    calling `resolve()`, which hands the answer back to the loop thread-safely."""

    # Signals carry the session KEY so handlers (bound methods → queued to the MAIN thread) know which
    # conversation they're for. CRITICAL: connect these to bound methods, never lambdas — a lambda has
    # no QObject receiver, so Qt runs it on the WORKER thread, and touching the GUI there crashes hard.
    step = Signal(str, object)               # key, agent_runner.Step
    approvalRequested = Signal(str, object)  # key, agent_runner.ApprovalRequest
    result = Signal(str, str, float)         # key, session_id, cost_usd
    error = Signal(str, str)                 # key, message
    done = Signal(str)                       # key

    def __init__(self, key: str, cwd: str, prompt: str, resume: str | None = None, bypass: bool = False,
                 model: str | None = None, effort: str | None = None, images: list | None = None):
        super().__init__()
        self.key = key
        self._cwd = cwd
        self._prompt = prompt
        self._images = list(images or [])     # fn + ⌃ + Space screenshots for this turn
        self._resume = resume
        self._bypass = bypass
        self._model = model
        self._effort = effort
        self._loop: asyncio.AbstractEventLoop | None = None
        self._approval_fut: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._runner = None

    def run(self):  # executes on the worker thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._go())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass                        # user pressed Stop — not an error
        except Exception as e:  # surface any failure instead of dying silently
            self.error.emit(self.key, str(e))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.done.emit(self.key)

    def stop(self):
        """Stop the run on ONE click: interrupt the current turn (SDK-sanctioned, responsive).
        Called from the UI thread; schedules the interrupt coroutine on the worker loop."""
        loop, runner = self._loop, self._runner
        if loop and runner:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(runner.interrupt()))

    def cancel(self):
        """Forceful fallback — cancel the task (exits the SDK client, kills the subprocess) if the
        interrupt didn't end the turn."""
        loop, task = self._loop, self._task
        if loop and task and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    async def _on_approval(self, req) -> bool:
        self._approval_fut = self._loop.create_future()
        self.approvalRequested.emit(self.key, req)
        return await self._approval_fut

    def resolve(self, approved: bool):
        """Called from the UI thread when the user taps Approve/Deny."""
        loop, fut = self._loop, self._approval_fut
        if loop and fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, approved)

    async def _go(self):
        from app.core.agent_runner import AgentRunner
        runner = AgentRunner(
            self._cwd,
            on_step=lambda s: self.step.emit(self.key, s),
            on_approval=self._on_approval,
            on_result=lambda sid, cost: self.result.emit(self.key, sid or "", float(cost or 0.0)),
        )
        self._runner = runner            # exposed so stop() can interrupt the live turn
        await runner.run(self._prompt, resume=self._resume, bypass=self._bypass,
                         model=self._model, effort=self._effort, images=self._images)


class _SessionLoader(QObject):
    """Scan ~/.claude/projects off the UI thread (hundreds of files) and hand back the list."""
    loaded = Signal(list)

    def run(self):
        try:
            from app.core.agent_sessions import list_sessions
            sessions = list_sessions(limit=300)
        except Exception:
            sessions = []
        self.loaded.emit(sessions)


class _SessionRow(QWidget):
    """A VoiceOS-style conversation row: a status dot + the title over a muted subtitle."""

    def __init__(self, title, subtitle, text_color, sub_color, dot_color="#6E6E78", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(9)
        dot = QLabel()
        dot.setFixedSize(7, 7)
        dot.setStyleSheet(f"background:{dot_color}; border-radius:3px;")
        lay.addWidget(dot, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet(f"color:{text_color}; font-size:12.5px; font-weight:600; background:transparent; border:none;")
        s = QLabel(subtitle)
        s.setStyleSheet(f"color:{sub_color}; font-size:10.5px; background:transparent; border:none;")
        col.addWidget(t)
        col.addWidget(s)
        lay.addLayout(col, 1)


import itertools as _itertools
_session_keys = _itertools.count(1)


class AgentSession:
    """One live (or just-finished) agent conversation: its worker thread plus all the state the notch
    and the left rail render from. `AssistantPage` keeps a registry of these and shows ("focuses") one
    at a time in the notch; the rest keep running in the background.

    `feed_lines` mirrors the notch Details feed so we can repaint it when this session is re-focused.
    """
    def __init__(self, key, cwd, prompt, title, bypass, model, effort, resume=None):
        import time
        self.key = key
        self.cwd = cwd
        self.prompt = prompt                 # this turn's input (seeds the Details feed)
        self.title = title                   # short conversation name (notch bubble + rail row + notif)
        self.bypass = bypass
        self.model = model
        self.effort = effort
        self.resume = resume                 # claude session id being resumed, or None for a new chat
        self.session_id = resume or ""       # real claude id (known for sure after the first result)
        self.status = "working"              # working | needs_you | done | error | stopped
        self.feed_lines: list[str] = []      # plain activity lines (the in-tab feed)
        self.entries: list[dict] = []        # the notch's rich Details: turns + actions (details_view)
        self.last_action = ""                # latest step label (for show_working on focus)
        self.result_text = ""                # what Claude generated (for the done card)
        self.cost = 0.0
        self.approval_req = None             # pending ApprovalRequest while status == needs_you
        self.pending_followup = None         # a follow-up spoken while this one was still running
        self.images: list[str] = []          # this turn's screenshots (fn + ⌃ + Space)
        self.pending_images: list[str] = []  # …and the queued follow-up's
        self.stopping = False                # user hit Stop on THIS session
        self.thread = None
        self.worker = None
        self.started_at = time.time()        # when this turn started (or, for a picked-up past chat, its last activity)
        self.duration = None                 # the finished turn's length in seconds (notch header timer)

    @property
    def running(self) -> bool:
        return self.thread is not None


class AssistantPage(QWidget):
    _voice_routed = Signal(object, str)   # (SessionInfo|None, task) — legacy route_speech path
    _voice_decided = Signal(object)       # {intent, idx, task, candidates, focused_key} from the router
    _recent_loaded = Signal(object)       # [SessionInfo] — past voice-agent chats for the rail
    _transcript_loaded = Signal(str, object)   # (session id, load_transcript() dict|None)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        # Concurrency: a registry of live/recent conversations; the notch shows the "focused" one at a
        # time, the rest keep running in the background (the left rail switches focus between them).
        self._sessions: dict[str, AgentSession] = {}
        self._focused_key: str | None = None
        self._recorder = None        # StreamingTranscriber while dictating a task
        self._recording = False
        self._overlay = None         # ambient notch (lazy; see _ensure_notch)
        self._rail = None            # left conversation rail (lazy; see _ensure_rail)
        self._resume_target = None   # {id, cwd, title} selected in the browser to continue
        self._recent = []            # past voice-agent chats on disk (rail history); see _load_recent
        self._all_sessions = []      # cached list for in-memory search filtering
        self._loader_thread = None
        try:                           # model + reasoning effort (picked on the notch card, persisted)
            from app.core.settings import settings as _s
            self._model = _s.assistant_model or "opus"      # a real model is always selected (no Auto)
            self._effort = _s.assistant_effort or "high"
        except Exception:
            self._model, self._effort = "opus", "high"
        self._build()
        self._load_sessions()
        self._voice_routed.connect(self._on_voice_routed)
        self._voice_decided.connect(self._on_voice_decided)
        self._recent_loaded.connect(self._on_recent_loaded)
        self._transcript_loaded.connect(self._on_transcript_loaded)
        self._load_recent()
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().aboutToQuit.connect(self.shutdown)
        except Exception:
            pass

    def shutdown(self):
        """App quit: end every running agent and wait for its thread. Without this, quitting mid-task
        (or in the first second, while past chats load) destroys a running QThread → Qt aborts with
        "QThread: Destroyed while thread is still running" → a "Filect quit unexpectedly" crash."""
        for sess in self._sessions.values():
            if sess.worker is not None:
                try:
                    sess.worker.cancel()            # exits the SDK client → kills the claude subprocess
                except Exception:
                    pass
        threads = [s.thread for s in self._sessions.values()] + [getattr(self, "_loader_thread", None)]
        for t in threads:
            if t is not None and t.isRunning():
                t.quit()
                t.wait(3000)                        # ponytail: a run that ignores cancel >3s still aborts
        self._last_notified_key = None   # which session the latest notification was about (click → focus)
        _tray = _get_or_create_tray()    # permanent Filect menu-bar icon + branded-notification channel
        if _tray is not None:
            try:
                _tray.messageClicked.connect(self._on_notification_clicked)
            except Exception:
                pass
        # Keep the notch (top) + rail (left) present at idle so they're summonable without a task first.
        QTimer.singleShot(1500, self._show_idle_overlays)

    def _show_idle_overlays(self):
        """When the app is open, show the notch nub (top) and rail strip (left) at idle — hover/click
        either to open it and start a task, without having run anything first."""
        try:
            self._refresh_rail()             # rail strip (empty → 'No active chats' + New chat)
        except Exception:
            pass
        try:
            self._notch("rest_idle")         # notch idle nub (faint hint; hover/click opens it)
        except Exception:
            pass

    # ---- session registry helpers ----
    @property
    def _focused(self) -> "AgentSession | None":
        return self._sessions.get(self._focused_key) if self._focused_key else None

    def _session_by_claude_id(self, sid: str | None):
        """The live/recent AgentSession whose claude session id matches (for routing a follow-up)."""
        if not sid:
            return None
        for s in self._sessions.values():
            if s.session_id == sid or s.resume == sid:
                return s
        return None

    def _any_running(self) -> bool:
        return any(s.running for s in self._sessions.values())

    # ---- ambient notch overlay (purely additive; never breaks the tab flow) ----
    def _ensure_notch(self):
        if self._overlay is not None:
            return self._overlay
        from app.ui.claude_overlay import ClaudeOverlay
        ov = ClaudeOverlay()
        ov.approve_clicked.connect(lambda: self._decide(True))
        ov.deny_clicked.connect(lambda: self._decide(False))
        ov.open_requested.connect(self._open_task)
        ov.dismissed.connect(self._on_notch_dismissed)
        ov.model_changed.connect(self._on_model_changed)
        ov.stop_clicked.connect(self._on_stop_task)
        ov.submit_text.connect(lambda t: self.run_voice_task(t, source="typed"))   # typed = same routing
        try:
            ov.set_model_effort(self._model, self._effort)   # reflect the saved choice on the pill
        except Exception:
            pass
        self._overlay = ov
        return ov

    def _on_notch_dismissed(self):
        """✕ on the notch clears the focus pointer — the next voice command starts fresh. The
        conversation itself keeps running (and stays in the rail), reopenable by clicking its row."""
        self._focused_key = None

    def _on_stop_task(self):
        """Stop on ONE click: interrupt the FOCUSED session's turn now, show immediate feedback, and
        force-cancel as a fallback if the interrupt didn't end it."""
        sess = self._focused
        if sess is None or not sess.running:
            return
        _trace(f"{sess.key} STOP requested '{sess.title}'")
        sess.stopping = True
        sess.worker.stop()                  # interrupt the live turn (responsive)
        self._notch("mark_stopping")        # disable the Stop button + show "Stopping…"
        self.status_label.setText("Stopping…")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2500, lambda k=sess.key: self._force_stop(k))

    def _force_stop(self, key):
        sess = self._sessions.get(key)
        if sess is not None and sess.running:
            sess.worker.cancel()            # forceful: cancel the task -> closes client -> kills subprocess

    def _on_model_changed(self, model: str, effort: str):
        """Model/effort picked on the notch card — persist it; applies to the next task/turn."""
        self._model = model or ""
        self._effort = effort or ""
        try:
            from app.core.settings import settings as _s
            _s.assistant_model = self._model
            _s.assistant_effort = self._effort
            _s._save_config()
        except Exception:
            pass

    def _tool_label(self, tool: str, detail: str) -> str:
        """'Writing index.html' from (tool, detail) — a readable action for the feed/notch."""
        verbs = {"Write": "Writing", "Edit": "Editing", "MultiEdit": "Editing", "NotebookEdit": "Editing",
                 "Read": "Reading", "Bash": "Running", "Grep": "Searching", "Glob": "Finding",
                 "WebFetch": "Fetching", "WebSearch": "Searching the web", "Task": "Delegating",
                 "TodoWrite": "Planning"}
        v = verbs.get(tool, f"Using {tool}")
        return f"{v} {detail}".strip() if detail else v

    def _notch(self, method: str, *args):
        """Call a ClaudeOverlay method if the notch is available — swallow any failure so the
        in-tab Assistant keeps working even if the overlay can't be created/shown."""
        try:
            ov = self._ensure_notch()
            getattr(ov, method)(*args)
        except Exception:
            pass

    def _open_task(self):
        """Notch "View task →" — open the ACTUAL Claude Code conversation in the Claude desktop app
        via its resume deep link (claude://resume?session=<id> -> importCliSession). Falls back to
        Filect's own Assistant tab when there's no session id (e.g. the task errored before saving)."""
        sid = (self._focused.session_id if self._focused else "") or ""
        if sid:
            try:
                import subprocess
                subprocess.Popen(["open", f"claude://resume?session={sid}"])
                return
            except Exception:
                pass
        try:
            self.mw.show()
            self.mw.raise_()
            self.mw.activateWindow()
            self.mw._on_nav_clicked(5)   # Assistant page index
        except Exception:
            pass

    # ---- UI ----
    def _build(self):
        # Two columns: left = conversation browser (click to resume), right = task + activity.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar(), 0)
        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(24, 24, 28, 24)
        right.setSpacing(14)
        self._build_main(right)
        outer.addWidget(right_w, 1)

    def _build_main(self, root):
        c = theme_manager.get_colors()
        title = QLabel("Assistant")
        title.setStyleSheet("font-size: 22px; font-weight: 700; background: transparent; border: none;")
        root.addWidget(title)
        sub = QLabel("Speak or type a task and a local Claude agent does it. Keep talking to follow "
                     "up on the same conversation, or pick one on the left to continue.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #8A8A92; font-size: 12.5px; background: transparent; border: none;")
        root.addWidget(sub)

        # "Continuing: <title>" banner — shown when a conversation is selected on the left.
        self.resume_banner = QFrame()
        self.resume_banner.setStyleSheet(
            "QFrame { background: rgba(217,119,87,0.12); border: 1px solid #D97757; border-radius: 8px; } "
            "QLabel { background: transparent; border: none; }")
        rb = QHBoxLayout(self.resume_banner)
        rb.setContentsMargins(12, 6, 8, 6)
        self.resume_label = QLabel("")
        self.resume_label.setStyleSheet("color:#E8A98C; font-size:12px; font-weight:600;")
        rb.addWidget(self.resume_label, 1)
        rb_x = QPushButton("✕")
        rb_x.setCursor(Qt.PointingHandCursor)
        rb_x.setFixedSize(20, 20)
        rb_x.setStyleSheet("QPushButton{background:transparent;border:none;color:#7A7A90;} "
                           "QPushButton:hover{color:#E8E8F0;}")
        rb_x.clicked.connect(self._clear_resume)
        rb.addWidget(rb_x)
        self.resume_banner.setVisible(False)
        root.addWidget(self.resume_banner)

        # Task input + Run
        self.task_input = QTextEdit()
        self.task_input.setPlaceholderText("e.g. In my project, find the TODO comments and list them")
        self.task_input.setFixedHeight(70)
        self.task_input.setStyleSheet(
            f"QTextEdit {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:10px; "
            f"padding:8px; font-size:13px; }}")
        root.addWidget(self.task_input)

        row = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#7A7A90; font-size:12px; background:transparent; border:none;")
        row.addWidget(self.status_label)
        row.addStretch()
        # Voice input — speak the task (reuses Filect's dictation backend)
        self._mic_idle_style = (
            "QPushButton { background:transparent; color:#D97757; border:1px solid #D97757; "
            "border-radius:9px; padding:0 16px; font-weight:600; } "
            "QPushButton:hover { background:rgba(217,119,87,0.08); } "
            "QPushButton:disabled { color:#E8C4B4; border-color:#E8C4B4; }")
        self._mic_rec_style = (
            "QPushButton { background:#E53935; color:white; border:none; border-radius:9px; "
            "padding:0 16px; font-weight:600; } QPushButton:hover { background:#D32F2F; }")
        self.mic_btn = QPushButton("🎤  Speak")
        self.mic_btn.setMinimumHeight(34)
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setStyleSheet(self._mic_idle_style)
        self.mic_btn.clicked.connect(self._toggle_mic)
        row.addWidget(self.mic_btn)
        self.run_btn = QPushButton("Run")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.setStyleSheet(
            "QPushButton { background:#D97757; color:white; border:none; border-radius:9px; "
            "padding:0 22px; font-weight:600; } QPushButton:hover { background:#C4633F; } "
            "QPushButton:disabled { background:#E8C4B4; }")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)
        root.addLayout(row)

        # Bypass toggle — like Claude Code's bypass-permissions mode: no Approve/Deny cards.
        self.bypass_check = QCheckBox("⚡  Bypass approvals — let it run without asking")
        self.bypass_check.setCursor(Qt.PointingHandCursor)
        self.bypass_check.setStyleSheet("color:#B26A00; font-size:12px; background:transparent; border:none;")
        self.bypass_check.setToolTip(
            "When on, the agent edits files and runs commands without asking you first — like "
            "Claude Code's bypass-permissions mode. Only use it for tasks you trust.")
        try:                                   # restore the saved choice (persists across restarts)
            from app.core.settings import settings as _s
            self.bypass_check.setChecked(bool(_s.assistant_bypass))
        except Exception:
            pass
        self.bypass_check.toggled.connect(self._save_bypass)
        root.addWidget(self.bypass_check)

        # Approval card (hidden until the agent asks)
        self.card = QFrame()
        self.card.setObjectName("approvalCard")
        self.card.setStyleSheet(
            f"QFrame#approvalCard {{ background:{c['surface']}; border:1px solid #D97757; border-radius:12px; }}"
            "QFrame#approvalCard QLabel { background:transparent; border:none; }")
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(16, 14, 16, 14)
        self.card_title = QLabel()
        self.card_title.setWordWrap(True)
        self.card_title.setStyleSheet("font-size:13px; font-weight:600;")
        card_lay.addWidget(self.card_title)
        self.card_detail = QLabel()
        self.card_detail.setWordWrap(True)
        self.card_detail.setStyleSheet("color:#7A7A90; font-size:12px; font-family:monospace;")
        card_lay.addWidget(self.card_detail)
        card_btns = QHBoxLayout()
        card_btns.addStretch()
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setMinimumHeight(30)
        self.deny_btn.setCursor(Qt.PointingHandCursor)
        self.deny_btn.setStyleSheet(
            "QPushButton { background:transparent; color:#D33; border:1px solid #D33; border-radius:8px; "
            "padding:0 16px; font-weight:600; } QPushButton:hover { background:rgba(221,51,51,0.08); }")
        self.deny_btn.clicked.connect(lambda: self._decide(False))
        card_btns.addWidget(self.deny_btn)
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setMinimumHeight(30)
        self.approve_btn.setCursor(Qt.PointingHandCursor)
        self.approve_btn.setStyleSheet(
            "QPushButton { background:#2F9E44; color:white; border:none; border-radius:8px; "
            "padding:0 16px; font-weight:600; } QPushButton:hover { background:#278238; }")
        self.approve_btn.clicked.connect(lambda: self._decide(True))
        card_btns.addWidget(self.approve_btn)
        card_lay.addLayout(card_btns)
        self.card.setVisible(False)
        root.addWidget(self.card)

        # Live transcript
        feed_label = QLabel("Activity")
        feed_label.setStyleSheet("color:#7A7A90; font-size:12px; font-weight:600; background:transparent; border:none;")
        root.addWidget(feed_label)
        self.feed = QTextEdit()
        self.feed.setReadOnly(True)
        self.feed.setStyleSheet(
            f"QTextEdit {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:10px; "
            f"padding:10px; font-size:13px; }}")
        root.addWidget(self.feed, 1)

    # ---- conversation browser (left column) ----
    def _build_sidebar(self):
        c = theme_manager.get_colors()
        frame = QFrame()
        frame.setFixedWidth(272)
        frame.setStyleSheet(
            f"QFrame {{ background:{c['surface']}; border:none; border-right:1px solid {c['border']}; }}")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 16, 12, 12)
        lay.setSpacing(10)

        # Header row: title + a compact "+ New" ghost button (cleaner than a full-width bar).
        hrow = QHBoxLayout()
        hrow.setContentsMargins(2, 0, 0, 0)
        hdr = QLabel("Conversations")
        hdr.setStyleSheet("font-size:13px; font-weight:700; letter-spacing:0.3px; "
                          "background:transparent; border:none;")
        hrow.addWidget(hdr)
        hrow.addStretch(1)
        self.new_btn = QPushButton("＋  New")
        self.new_btn.setCursor(Qt.PointingHandCursor)
        self.new_btn.setFixedHeight(26)
        self.new_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#D97757;border:1px solid rgba(217,119,87,0.55);"
            "border-radius:8px;font-weight:600;font-size:11.5px;padding:0 10px;} "
            "QPushButton:hover{background:rgba(217,119,87,0.12);border-color:#D97757;}")
        self.new_btn.clicked.connect(self._clear_resume)
        hrow.addWidget(self.new_btn)
        lay.addLayout(hrow)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search conversations…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedHeight(32)
        self.search_box.setStyleSheet(
            f"QLineEdit{{background:{c['bg']};border:1px solid {c['border']};border-radius:9px;"
            "padding:4px 10px;font-size:12px;} QLineEdit:focus{border-color:rgba(217,119,87,0.6);}")
        self.search_box.textChanged.connect(self._on_search)
        lay.addWidget(self.search_box)

        self.session_list = QListWidget()
        self.session_list.setSpacing(1)
        self.session_list.setStyleSheet(
            "QListWidget{background:transparent;border:none;outline:none;} "
            "QListWidget::item{padding:1px;border-radius:9px;} "
            "QListWidget::item:hover{background:rgba(217,119,87,0.10);} "
            "QListWidget::item:selected{background:rgba(217,119,87,0.20);}")
        self.session_list.itemClicked.connect(self._on_session_clicked)
        lay.addWidget(self.session_list, 1)

        self._sessions_hint = QLabel("Loading…")
        self._sessions_hint.setStyleSheet("color:#7A7A90;font-size:11px;background:transparent;border:none;")
        lay.addWidget(self._sessions_hint)
        return frame

    def _load_sessions(self):
        self._loader_thread = QThread(self)
        self._loader = _SessionLoader()
        self._loader.moveToThread(self._loader_thread)
        self._loader_thread.started.connect(self._loader.run)
        self._loader.loaded.connect(self._on_sessions_loaded)
        self._loader.loaded.connect(self._loader_thread.quit, Qt.DirectConnection)  # ends the thread the moment it's loaded, no UI round-trip
        self._loader_thread.start()

    def _on_sessions_loaded(self, sessions):
        # Hide router-query junk + stubs so the browser shows real conversations only.
        self._all_sessions = [s for s in (sessions or []) if not self._junk_session(s)]
        self._populate_list(self._all_sessions)

    def _populate_list(self, sessions):
        self.session_list.clear()
        c = theme_manager.get_colors()
        active = self._focused.session_id if self._focused else None
        for s in sessions:
            it = QListWidgetItem()
            it.setToolTip(f"{s.project} · {s.when}\n{s.preview}")
            it.setData(Qt.UserRole, (s.id, s.cwd, s.title))
            dot = "#D97757" if active and s.id == active else "#6E6E78"   # orange = live now
            row = _SessionRow(s.title, f"{s.project}  ·  {s.when}", c['text'], "#8A8A92", dot)
            it.setSizeHint(row.sizeHint())
            self.session_list.addItem(it)
            self.session_list.setItemWidget(it, row)
        n = len(self._all_sessions)
        shown = len(sessions)
        if shown == n:
            self._sessions_hint.setText(f"{n} conversation{'s' if n != 1 else ''}")
        else:
            self._sessions_hint.setText(f"{shown} of {n}")

    def _on_search(self, text):
        q = (text or "").strip().lower()
        if not q:
            self._populate_list(self._all_sessions)
            return
        hits = [s for s in self._all_sessions
                if q in s.title.lower() or q in s.preview.lower() or q in s.project.lower()]
        self._populate_list(hits)

    def _on_session_clicked(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        sid, cwd, title = data
        live = self._session_by_claude_id(sid)
        if live is not None:                 # already in play → focus it (becomes the pointer)
            self._focus(live.key)
        self._resume_target = {"id": sid, "cwd": cwd, "title": title}
        self.resume_label.setText(f"↻  Continuing:  {title}")
        self.resume_banner.setVisible(True)
        self.task_input.setFocus()

    def _clear_resume(self):
        """"+ New": clear the browser selection and the focus pointer so the next task starts fresh.
        Running conversations keep running (and stay in the rail)."""
        self._resume_target = None
        self._focused_key = None
        self.resume_banner.setVisible(False)
        self.session_list.clearSelection()
        self.task_input.clear()
        self.task_input.setFocus()

    def _save_bypass(self, on: bool):
        """Persist the Bypass toggle so it survives app restarts (the user flagged it resetting)."""
        try:
            from app.core.settings import settings as _s
            _s.assistant_bypass = bool(on)
            _s._save_config()
        except Exception:
            pass

    # ---- actions ----
    def _append(self, text: str):
        self.feed.append(text)

    @staticmethod
    def _junk_session(ss) -> bool:
        """True for saved 'sessions' that aren't real conversations: the intent router's own
        classification queries, resumed-context stubs, and empty titles. These must never be offered
        as a RESUME target (and shouldn't clutter the browser)."""
        tl = (getattr(ss, "title", "") or "").strip().lower()
        return (not tl or tl.startswith("current conversation:") or "spoken command:" in tl
                or tl.startswith("this session is being continued"))

    def _build_voice_candidates(self, text: str):
        """The conversations the router may RESUME: live chats first, then clean recent saved ones,
        then OLDER ones whose title/preview matches what you NAMED — so 'use my Hyper3D MCP
        conversation' can reach a chat from days ago among hundreds. Junk (router queries, dupes,
        the dev session) is filtered out; without this the router only ever saw the 20 newest."""
        import re
        cands, seen_ids, seen_titles = [], set(), set()

        def _add(kind, key, sid, cwd, title):
            t = (title or "").strip()
            k = t.lower()[:60]
            if (sid and sid in seen_ids) or (k and k in seen_titles):
                return
            if sid:
                seen_ids.add(sid)
            if k:
                seen_titles.add(k)
            cands.append({"kind": kind, "key": key, "id": sid, "cwd": cwd, "title": t})

        for s in sorted(self._sessions.values(), key=lambda s: s.started_at, reverse=True):
            _add("live", s.key, s.session_id or s.resume, s.cwd, s.title)
        try:
            from app.core.agent_sessions import list_sessions
            from app.core.agent_sessions import _TITLE_FILLERS as _fill
            saved = [ss for ss in list_sessions(limit=500) if not self._junk_session(ss)]
        except Exception:
            saved, _fill = [], set()
        for ss in saved[:12]:                              # recent real conversations (newest first)
            _add("saved", None, ss.id, ss.cwd, ss.title)
        words = {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
                 if len(w) > 2 and w not in _fill}
        scored = []
        for ss in saved:                                   # name/topic matches — reach OLD convos
            hay = (ss.title + " " + ss.preview).lower()
            sc = sum(1 for w in words if w in hay)
            if sc >= 2:                                    # need real overlap, not one stray word
                scored.append((sc, ss.mtime, ss))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        for _, _, ss in scored[:6]:
            _add("saved", None, ss.id, ss.cwd, ss.title)
        return cands

    def run_voice_task(self, text: str, source: str = "voice", images=None):
        """Fn+Control entry. Route the spoken command to a NEW or RESUMED conversation via one
        quick LLM call (off the UI thread), then run it. The notch shows "Routing…" meanwhile, then
        names the one it picked. Falls back to a new conversation on any routing failure."""
        text = (text or "").strip()
        if not text:
            return
        # Intent routing on the Claude subscription (Opus): decide NEW vs FOLLOW_UP (continue the
        # focused conversation — the "that one" pointer) vs RESUME a named one. Runs off the UI thread
        # (~2.5s warm); the notch shows "Routing…" meanwhile. ANY failure falls back safely.
        focused = self._focused
        focused_title = focused.title if focused else None
        candidates = self._build_voice_candidates(text)
        images = list(images or [])
        _trace(f"{source} heard: {text!r} | focused={focused_title!r} | screenshots={len(images)} | "
               f"candidates={[c['title'] for c in candidates]}")
        self._notch("present")
        self._notch("set_title", "Routing…")
        self._notch("show_working", "", "")
        self._notch("flash")
        import threading

        def worker():
            cand_titles = [c["title"] for c in candidates]
            try:
                from app.core.intent_router import IntentRouter
                d = IntentRouter.get().classify(text, focused_title, cand_titles)
                intent, idx, task = d["intent"], d["index"], d["task"]
            except Exception as e:                     # safe fallback: continue focused, else new
                intent, idx, task = ("FOLLOW_UP" if focused_title else "NEW"), None, text
                _trace(f"router FAILED ({e}) -> fallback {intent}")
            self._voice_decided.emit({"intent": intent, "idx": idx, "task": task,
                                      "candidates": candidates, "images": images,
                                      "focused_key": focused.key if focused else None})
        threading.Thread(target=worker, daemon=True, name="filect-assistant-route").start()

    def _on_voice_decided(self, result):
        """Router decision (UI thread): continue the focused chat, resume a named one, or start new."""
        intent, idx, task = result["intent"], result["idx"], result["task"]
        cands, fkey = result["candidates"], result["focused_key"]
        images = result.get("images") or []
        _trace(f"decided: {intent} idx={idx} task={task!r}")

        def _continue(sess, title):
            self._focus(sess.key)
            self._notch("set_title", f"↳ continuing “{title}”")   # visible cue (correctable)
            if sess.running:
                sess.pending_followup, sess.pending_images = task, images
                _trace(f"  -> queue follow-up on {sess.key} '{title}' (running)")
                self._notch("show_working", "Queued — will continue when this step finishes")
                self._notch("flash")
            else:
                _trace(f"  -> continue {sess.key} '{title}' (was idle)")
                self.run_task(task, resume=sess.session_id or sess.resume, cwd=sess.cwd, images=images)

        if intent == "FOLLOW_UP" and fkey and fkey in self._sessions:
            _continue(self._sessions[fkey], self._sessions[fkey].title)
            return
        if intent == "RESUME" and isinstance(idx, int) and 0 <= idx < len(cands):
            c = cands[idx]
            if c["kind"] == "live" and c["key"] in self._sessions:
                _continue(self._sessions[c["key"]], c["title"])
            else:
                self._resume_target = {"id": c["id"], "cwd": c["cwd"], "title": c["title"]}
                _trace(f"  -> resume saved '{c['title']}' id={(c['id'] or '')[:8]}")
                self._notch("set_title", f"↳ continuing “{c['title']}”")
                self.run_task(task, resume=c["id"], cwd=c["cwd"], images=images)
            return
        # NEW (or anything unresolved) → a fresh conversation.
        _trace("  -> NEW conversation")
        self._resume_target = None
        self.run_task(task, images=images)

    def _on_voice_routed(self, session, task):
        """Routing finished (UI thread): resume the matched conversation, or start a new one."""
        if session is not None:
            self._resume_target = {"id": session.id, "cwd": session.cwd, "title": session.title}
            self.resume_label.setText(f"↻  Continuing:  {session.title}")
            self.resume_banner.setVisible(True)
            self._notch("set_title", f"Continuing “{session.title}”")
            self.run_task(task, resume=session.id, cwd=session.cwd)
        else:
            self._resume_target = None   # honour the "new" decision over any stale selection
            self.resume_banner.setVisible(False)
            self.run_task(task)

    def _run(self):
        self.run_task(self.task_input.toPlainText(), self.bypass_check.isChecked())

    def run_task(self, prompt: str, bypass: bool | None = None,
                 resume: str | None = None, cwd: str | None = None, images=None):
        """Start a NEW agent conversation, or continue an existing one. New tasks each get their own
        AgentSession and run CONCURRENTLY. Resuming REUSES that conversation's AgentSession: queued as
        a follow-up if it's still running, or a fresh turn on the same claude session if it's idle — so
        one conversation is one entry for its whole life. Shared by Run, voice, and the left rail."""
        prompt = (prompt or "").strip()
        if not prompt:
            return
        if bypass is None:
            bypass = self.bypass_check.isChecked()
        if resume is None and self._resume_target:        # browser selection is the resume target
            resume = self._resume_target["id"]
            cwd = cwd or self._resume_target["cwd"]
        if resume:
            existing = self._session_by_claude_id(resume)
            if existing is not None and existing.running:  # still working → queue a follow-up on it
                existing.pending_followup, existing.pending_images = prompt, list(images or [])
                _trace(f"run_task: queue follow-up on {existing.key} '{existing.title}' (running)")
                self._focus(existing.key)
                self._notch("show_working", "Queued — will continue when this step finishes")
                self._notch("flash")
                return
            if existing is not None:                       # idle/finished → new turn, same session
                self._restart_session(existing, prompt, bypass, images)
                return
        import os
        workdir = cwd or DEFAULT_WORKSPACE
        if cwd is None:
            os.makedirs(workdir, exist_ok=True)       # only auto-create our OWN default workspace
        from app.core.agent_sessions import generate_title
        display = ((self._resume_target or {}).get("title") or generate_title(prompt)) if resume \
            else generate_title(prompt)               # new task names itself locally (instant, no AI)
        key = f"s{next(_session_keys)}"
        sess = AgentSession(key, workdir, prompt, display, bypass,
                            self._model or None, self._effort or None, resume=resume)
        sess.images = list(images or [])
        self._sessions[key] = sess
        self._start_worker(sess, resume)

    def _restart_session(self, sess, prompt, bypass, images=None):
        """Continue a finished conversation with a new turn, REUSING its AgentSession (one conversation
        = one entry over its whole life — no duplicate rail slot)."""
        import time
        sess.prompt, sess.bypass, sess.images = prompt, bypass, list(images or [])
        sess.status, sess.result_text, sess.cost, sess.approval_req = "working", "", 0.0, None
        sess.started_at, sess.duration = time.time(), None
        _trace(f"restart {sess.key} '{sess.title}' (new turn, resume={sess.session_id or sess.resume})")
        self._start_worker(sess, sess.session_id or sess.resume)

    def _start_worker(self, sess, resume_id):
        """Build + start this session's worker thread, wire its signals (keyed by session), focus it."""
        if self.task_input.toPlainText().strip() != sess.prompt:
            self.task_input.setPlainText(sess.prompt)   # reflect a voice task in the box
        self.run_btn.setEnabled(False)
        self.status_label.setText("Running…")
        sess.thread = QThread(self)
        sess.worker = AgentWorker(sess.key, sess.cwd, sess.prompt, resume=resume_id, bypass=sess.bypass,
                                  model=sess.model, effort=sess.effort, images=sess.images)
        sess.worker.moveToThread(sess.thread)
        sess.thread.started.connect(sess.worker.run)
        # BOUND methods, never lambdas: Qt queues a bound-method slot to its receiver's (main) thread,
        # so every GUI update happens on the main thread. The session key now rides in each signal, so
        # it needn't be baked into the connection. (A lambda here has no QObject receiver → Qt runs it
        # on the WORKER thread → GUI/CGS calls off the main thread → the hard crash we just saw.)
        sess.worker.step.connect(self._on_step)
        sess.worker.approvalRequested.connect(self._on_approval)
        sess.worker.result.connect(self._on_result)
        sess.worker.error.connect(self._on_error)
        sess.worker.done.connect(self._on_done)
        _trace(f"spawn {sess.key} '{sess.title}' cwd={sess.cwd} resume={resume_id or '(new)'} "
               f"model={sess.model} bypass={sess.bypass} | live now: {sorted(self._sessions)}")
        sess.thread.start()
        self._focus(sess.key, fresh=True)

    # ---- focus: which conversation the notch shows (and voice follow-ups target) ----
    def _focus(self, key: str, fresh: bool = False):
        """Make `key` the conversation shown in the notch and the pointer voice follow-ups target.
        `fresh` = a task just started (seed the feed); otherwise repaint from stored state."""
        sess = self._sessions.get(key)
        if sess is None:
            return
        self._focused_key = key
        _trace(f"focus -> {key} '{sess.title}' fresh={fresh} status={sess.status}")
        if fresh:
            self.feed.clear()
            if sess.session_id or sess.resume:
                self._append(f"↻ continuing session {(sess.session_id or sess.resume)[:8]}…\n")
            self._append(f"▶ {sess.prompt}\n")
            if sess.images:
                self._append(f"🖼 {len(sess.images)} screenshot(s) attached\n")
            if sess.bypass:
                self._append("⚡ Bypass mode: running without approval prompts.\n")
            self._notch("present")                  # resets the notch Details feed
            self._notch("set_title", sess.prompt)   # seed the feed with this turn's full input…
            self._notch("set_title", sess.title)    # …but show the short title on the bubble
            self._notch("show_working", "Starting…", "")
            self._notch("flash")                    # brief 'receipt' so the trigger is visibly confirmed
            sess.feed_lines = [f"▸ {sess.prompt}"]  # mirror what the notch feed now holds
            # Rich Details keep the whole conversation: a continued chat shows its earlier turns too.
            from app.ui import details_view
            details_view.add_step(sess.entries, "user", text=sess.prompt, images=sess.images)
            self._notch("set_details", sess.entries)
        else:
            self._render_focused()
        self._refresh_rail()

    def _render_focused(self):
        """Repaint the notch + in-tab feed from the focused session's stored state (on focus-swap)."""
        sess = self._focused
        if sess is None:
            return
        self._notch("present")
        self._notch("load_state", sess.title, list(sess.feed_lines), sess.status,
                    sess.last_action, sess.approval_req, sess.result_text, sess.cost,
                    sess.session_id, sess.prompt, sess.started_at, sess.duration, sess.entries)
        self.feed.clear()
        self._append(f"▶ {sess.prompt}\n")
        for ln in sess.feed_lines[1:]:              # [0] is the "▸ prompt" seed, already shown above
            self._append(f"  {ln}")

    def _ensure_rail(self):
        """The left conversation rail (lazy, like the notch). Returns None if it can't be created."""
        if getattr(self, "_rail", None) is not None:
            return self._rail
        self._rail = None
        try:
            from app.ui.conversation_rail import ConversationRail
            rail = ConversationRail()
            rail.session_clicked.connect(self._on_rail_clicked)
            rail.new_requested.connect(self._clear_resume)
            rail.recent_clicked.connect(self._on_recent_clicked)
            self._rail = rail
        except Exception:
            pass
        return self._rail

    def _on_rail_clicked(self, key):
        """A rail row from this run — show that conversation in the notch (live if it's running)."""
        if self._sessions.get(key) is None:
            return
        _trace(f"rail click -> show {key} '{self._sessions[key].title}'")
        self._show_in_notch(key)

    def _show_in_notch(self, key):
        """Selecting a conversation drops the notch down showing it (VoiceOS behaviour): it becomes the
        focus pointer + resume target, and the notch opens on it. Only a conversation SELECT does this —
        clicking the rail strip or the notch itself never opens the other."""
        sess = self._sessions[key]
        self._focus(key)
        self._notch("open_full")
        self._resume_target = {"id": sess.session_id or sess.resume, "cwd": sess.cwd, "title": sess.title}

    def _on_recent_clicked(self, sid):
        """A past chat from disk — load its transcript off the UI thread (_on_transcript_loaded shows it).
        Already picked up this run → just show it."""
        live = self._session_by_claude_id(sid)
        if live is not None:
            self._show_in_notch(live.key)
            return
        info = next((r for r in self._recent if r.id == sid), None)
        if info is None:
            return
        import threading

        def worker():
            try:
                from app.core.agent_sessions import load_transcript
                data = load_transcript(sid, info.cwd)
            except Exception:
                data = None
            try:
                self._transcript_loaded.emit(sid, data)
            except RuntimeError:                    # page torn down before the read finished (quit)
                pass
        threading.Thread(target=worker, daemon=True, name="filect-assistant-transcript").start()

    def _on_transcript_loaded(self, sid, data):
        """Pick a past chat back up and show it: it becomes a finished (not running) conversation with its
        real transcript, and the focus pointer — so your next spoken or typed command continues THIS
        chat (the router follows the focused session → FOLLOW_UP → run_task resumes the same Claude
        session). Its time stays its last activity, so just viewing it doesn't bump it to Today."""
        live = self._session_by_claude_id(sid)       # a double-click loads twice — show, don't duplicate
        if live is not None:
            self._show_in_notch(live.key)
            return
        info = next((r for r in self._recent if r.id == sid), None)
        if info is None:
            return
        lines, prompt, result, entries = self._transcript_view(data) if data else ([], "", "", [])
        prompt = prompt or info.preview or info.title
        sess = AgentSession(f"s{next(_session_keys)}", info.cwd, prompt, info.title,
                            self.bypass_check.isChecked(), self._model or None, self._effort or None,
                            resume=sid)
        sess.status, sess.result_text = "done", result
        sess.feed_lines = lines or [f"▸ {prompt}"]
        sess.entries = entries or [{"id": "0", "kind": "user", "text": prompt}]
        sess.started_at, sess.duration = info.mtime, (data or {}).get("duration")
        self._sessions[sess.key] = sess
        _trace(f"recent click -> show {sess.key} '{sess.title}' id={sid[:8]} ({len(sess.feed_lines)} lines)")
        self._show_in_notch(sess.key)

    def _transcript_view(self, data):
        """(feed lines, last prompt, last turn's replies, rich entries) for a saved conversation, in the
        SAME format live tasks use — '▸ what you said', readable tool labels ('Writing index.html'),
        Claude's text as-is; the entries carry each action's input + result for the rich Details — so a
        past chat reads exactly like one you watched run."""
        from app.core.agent_runner import _tool_detail
        from app.ui import details_view
        lines, prompt, result, entries = [], "", [], []
        for st in data.get("steps", []):
            if st[0] == "result":                    # ✓/✗ + output for its action (entries only)
                details_view.add_step(entries, "result", tool_id=st[1], text=st[2], is_error=st[3])
            elif st[0] == "user":
                prompt, result = st[1], []           # result = the LAST turn's replies, like live
                lines.append(f"▸ {st[1]}")
                details_view.add_step(entries, "user", text=st[1])
            elif st[0] == "text":
                result.append(st[1])
                lines.append(st[1])
                details_view.add_step(entries, "text", text=st[1])
            else:
                label = self._tool_label(st[1], _tool_detail(st[1], st[2]))
                if not lines or lines[-1] != label:
                    lines.append(label)
                details_view.add_step(entries, "tool", tool=st[1], input=st[2],
                                      tool_id=st[3] if len(st) > 3 else "")
        return lines[-300:], prompt, " ".join(result), entries   # ponytail: in-tab feed keeps the last 300 lines

    def _load_recent(self):
        """Read past voice-agent chats (the agent's workspace folder) off the UI thread — it touches
        disk — and hand them back through a signal to a bound method, so the rail updates on the main
        thread."""
        import threading

        def worker():
            try:
                from app.core.agent_sessions import list_sessions, remembered_sessions
                # + chats continued outside the workspace (a resumed project chat), else they'd
                # vanish from the rail after a restart
                found = list_sessions(limit=300, cwd=DEFAULT_WORKSPACE) + remembered_sessions(DEFAULT_WORKSPACE)
                seen, items = set(), []
                for s in found:
                    if s.id not in seen and not self._junk_session(s):   # the rail keeps the last 30 days
                        seen.add(s.id)
                        items.append(s)
                items.sort(key=lambda s: s.mtime, reverse=True)
            except Exception:
                items = []
            try:
                self._recent_loaded.emit(items)
            except RuntimeError:                    # page torn down before the read finished (quit)
                pass
        threading.Thread(target=worker, daemon=True, name="filect-assistant-recent").start()

    def _on_recent_loaded(self, items):
        self._recent = list(items or [])
        if self._rail is not None:          # else the idle overlays (or the first task) will show it
            self._refresh_rail()

    def _refresh_rail(self, key: str | None = None):
        rail = self._ensure_rail()
        if rail is None:
            return
        # Past chats not already in this run's sessions (a picked-up or resumed chat is shown once).
        live_ids = {s.session_id or s.resume for s in self._sessions.values()} - {None, ""}
        recent = [r for r in self._recent if r.id not in live_ids]
        try:
            rail.update_sessions(list(self._sessions.values()), self._focused_key, recent)
        except Exception:
            pass

    # ---- voice input ----
    def _toggle_mic(self):
        if self._recording:
            self._stop_mic()
        else:
            self._start_mic()

    def _start_mic(self):
        try:
            from app.core.transcription import StreamingTranscriber
            from app.core.settings import settings
        except Exception as e:
            self.status_label.setText(f"Voice unavailable: {e}")
            return
        try:
            self._recorder = StreamingTranscriber(
                terms=settings.dictation_custom_terms,
                language=(settings.dictation_language or None))
            self._recorder.finished.connect(self._on_transcript)
            self._recorder.error.connect(self._on_mic_error)
            self._recording = True
            self.mic_btn.setText("●  Recording… tap to stop")
            self.mic_btn.setStyleSheet(self._mic_rec_style)
            self._recorder.start()
        except Exception as e:
            self._on_mic_error(str(e))

    def _stop_mic(self):
        self._recording = False
        self.mic_btn.setText("Transcribing…")
        self.mic_btn.setEnabled(False)
        r = self._recorder
        if r is not None:
            try:
                r.stop_recording()
            except Exception as e:
                self._on_mic_error(str(e))

    def _on_transcript(self, text: str):
        text = (text or "").strip()
        if text:
            cur = self.task_input.toPlainText().strip()
            self.task_input.setPlainText((cur + " " + text).strip() if cur else text)
        self._reset_mic()

    def _on_mic_error(self, msg: str):
        self.status_label.setText(f"Voice error: {msg}")
        self._reset_mic()

    def _reset_mic(self):
        self._recording = False
        self._recorder = None
        self.mic_btn.setEnabled(True)
        self.mic_btn.setText("🎤  Speak")
        self.mic_btn.setStyleSheet(self._mic_idle_style)

    def _on_step(self, key, step):
        sess = self._sessions.get(key)
        if sess is None:
            return
        focused = (key == self._focused_key)
        from app.ui import details_view
        if step.kind == "tool":
            label = self._tool_label(step.tool, getattr(step, "detail", ""))
            sess.last_action, sess.status = label, "working"
            _trace(f"{key} · {label}")
            if not sess.feed_lines or sess.feed_lines[-1] != label:
                sess.feed_lines.append(label)
            details_view.add_step(sess.entries, "tool", tool=step.tool, input=getattr(step, "input", None),
                                  tool_id=getattr(step, "tool_id", ""))
            if focused:
                self._append(f"  → {label}")
                self._notch("set_details", sess.entries)
                self._notch("show_working", label, "")    # the readable action line
        elif step.kind == "text" and step.text:
            sess.result_text = (sess.result_text + " " + step.text).strip()
            sess.status = "working"
            if not sess.feed_lines or sess.feed_lines[-1] != step.text:
                sess.feed_lines.append(step.text)
            details_view.add_step(sess.entries, "text", text=step.text)
            if focused:
                self._append(step.text)
                self._notch("set_details", sess.entries)
                self._notch("show_working", step.text, "")
        elif step.kind == "result":                  # attaches ✓/✗ + output to its action
            details_view.add_step(sess.entries, "result", text=step.text, tool_id=step.tool_id,
                                  is_error=getattr(step, "is_error", False))
            if getattr(step, "is_error", False):
                _trace(f"{key} ✗ {step.text[:120]!r}")
            if focused:
                self._notch("set_details", sess.entries)
        self._refresh_rail(key)

    def _on_approval(self, key, req):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.approval_req, sess.status = req, "needs_you"
        _trace(f"{key} NEEDS APPROVAL: {req.tool} (focused={key == self._focused_key})")
        self._refresh_rail(key)
        if key == self._focused_key:
            self.card_title.setText(f"The assistant wants to use:  {req.tool}")
            detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in (req.tool_input or {}).items())
            self.card_detail.setText(detail or "(no details)")
            self.card.setVisible(True)
            self._notch("show_needs_you", req.tool, req.tool_input)
        else:                               # a backgrounded agent is blocked — ping so it isn't missed
            self._notify_needs_you(sess)

    def _decide(self, approved: bool):
        """Approve/Deny from the notch or the in-tab card — answers the FOCUSED session's gate."""
        sess = self._focused
        if sess is None or sess.approval_req is None:
            return
        _trace(f"{sess.key} {'APPROVED' if approved else 'DENIED'}")
        sess.approval_req, sess.status = None, "working"
        self.card.setVisible(False)
        self._append(f"  {'✓ approved' if approved else '✗ denied'}")
        if sess.worker:
            sess.worker.resolve(approved)
        self._refresh_rail(sess.key)

    def _on_result(self, key, session_id: str, cost: float):
        sess = self._sessions.get(key)
        if sess is None:
            return
        if session_id:                              # real claude id — so a follow-up continues it
            sess.session_id = session_id
            if sess.cwd and _os.path.normpath(sess.cwd) != _os.path.normpath(DEFAULT_WORKSPACE):
                try:                                # a chat continued in another project: keep it
                    from app.core.agent_sessions import remember_session   # on the rail after restarts
                    remember_session(session_id, sess.cwd)
                except Exception:
                    pass
        sess.cost, sess.status = float(cost or 0.0), "done"
        import time
        sess.duration = time.time() - sess.started_at
        _trace(f"{key} DONE '{sess.title}' sid={session_id or '(none)'} cost=${float(cost or 0):.4f} "
               f"focused={key == self._focused_key}")
        if key == self._focused_key:
            self.status_label.setText(f"Done — saved as session {session_id[:8]}…" if session_id
                                      else "Done")
            self._notch("show_done", session_id, cost, sess.prompt, sess.result_text)
        self._refresh_rail(key)
        if not sess.stopping:                       # a user-initiated Stop isn't a "finished" event
            _play_chime(True)
            if key != self._focused_key or not _app_is_active():   # not looking at THIS one → surface it
                self._last_notified_key = key
                _trace(f"NOTIFY done: {sess.title!r}")
                _notify("Filect · task finished", sess.title or "Done")

    def _on_error(self, key, msg: str):
        sess = self._sessions.get(key)
        if sess is None:
            return
        sess.status, sess.result_text = "error", msg
        _trace(f"{key} ERROR '{sess.title}': {msg}")
        if key == self._focused_key:
            self._append(f"\n⚠ error: {msg}")
            self.status_label.setText("Error")
            self._notch("show_error", msg)
        self._refresh_rail(key)
        _play_chime(False)
        if key != self._focused_key or not _app_is_active():
            self._last_notified_key = key
            _trace(f"NOTIFY error: {sess.title!r}")
            _notify("Filect · task failed", msg)

    def _on_done(self, key):
        sess = self._sessions.get(key)
        if sess is None:
            return
        _trace(f"{key} worker ended (stopping={sess.stopping}, "
               f"pending_followup={bool(sess.pending_followup)}, still running: "
               f"{[k for k, s in self._sessions.items() if s.running and k != key]})")
        if sess.thread:                             # tear down THIS session's worker thread
            sess.thread.quit()
            sess.thread.wait(3000)
        sess.thread = None
        sess.worker = None
        if not self._any_running():
            self.run_btn.setEnabled(True)
        if sess.stopping:                           # user hit Stop — don't fire any queued follow-up
            sess.stopping = False
            sess.pending_followup = None
            sess.status = "stopped"
            if key == self._focused_key:
                self.card.setVisible(False)
                self.status_label.setText("Stopped")
                self._notch("show_done", "", 0.0, "", "Stopped by you.")
            self._refresh_rail(key)
            return
        # A follow-up was spoken while this was running — send it now, continuing the SAME chat.
        if sess.pending_followup:
            text, sess.pending_followup = sess.pending_followup, None
            imgs, sess.pending_images = sess.pending_images, []
            self.run_task(text, resume=sess.session_id or sess.resume, cwd=sess.cwd, images=imgs)
            return
        if key == self._focused_key:
            self.card.setVisible(False)
        self._refresh_rail(key)

    def _notify_needs_you(self, sess):
        """A backgrounded (non-focused) agent hit an approval gate — ping so it doesn't stall unseen.
        Clicking the notification focuses that session (see _on_notification_clicked)."""
        _play_chime(False)
        if not _app_is_active() or sess.key != self._focused_key:
            tool = getattr(sess.approval_req, "tool", "") if sess.approval_req else ""
            self._last_notified_key = sess.key
            _trace(f"NOTIFY needs-you: {sess.title!r} ({tool})")
            _notify("Filect · needs your approval", f"{sess.title}: {tool}".strip(" :"))

    def _on_notification_clicked(self):
        """A branded notification was clicked (tray path) — focus the session it was about and bring
        Filect forward. QSystemTrayIcon.messageClicked doesn't say WHICH message, so we focus the most
        recently notified one."""
        key = self._last_notified_key
        _trace(f"notification clicked -> focus {key}")
        if key and key in self._sessions:
            self._focus(key)
        try:
            self.mw.show()
            self.mw.raise_()
            self.mw.activateWindow()
        except Exception:
            pass
