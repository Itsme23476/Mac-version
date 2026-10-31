"""Filect "Assistant" page.

Type a task, hit Run, and a local Claude agent carries it out — you watch each step stream in
live, and whenever it wants to do something risky (write a file, run a command) a card asks you
to Approve or Deny. Each task is a real, saved Claude session (resumable, and visible in Claude
Desktop). The agent work runs on a worker thread; this file bridges it to the Qt UI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton, QFrame, QScrollArea, QCheckBox,
    QLineEdit, QListWidget, QListWidgetItem,
)

from app.ui.theme_manager import theme_manager

# Where the assistant works by default. A dedicated folder keeps it out of the way and makes the
# saved sessions easy to find. (Letting the user pick the folder / resume a specific session is the
# next increment.)
DEFAULT_WORKSPACE = str(Path.home() / "Filect Assistant")


class AgentWorker(QObject):
    """Runs one AgentRunner task inside an asyncio loop on a worker thread, and relays everything to
    the UI via Qt signals. Approvals are the tricky bit: the agent's async permission callback emits
    `approvalRequested` and then *awaits* a future; the UI resolves it (from the main thread) by
    calling `resolve()`, which hands the answer back to the loop thread-safely."""

    step = Signal(object)              # agent_runner.Step
    approvalRequested = Signal(object)  # agent_runner.ApprovalRequest
    result = Signal(str, float)        # session_id, cost_usd
    error = Signal(str)
    done = Signal()

    def __init__(self, cwd: str, prompt: str, resume: str | None = None, bypass: bool = False,
                 model: str | None = None, effort: str | None = None):
        super().__init__()
        self._cwd = cwd
        self._prompt = prompt
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
            self.error.emit(str(e))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.done.emit()

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
        self.approvalRequested.emit(req)
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
            on_step=lambda s: self.step.emit(s),
            on_approval=self._on_approval,
            on_result=lambda sid, cost: self.result.emit(sid or "", float(cost or 0.0)),
        )
        self._runner = runner            # exposed so stop() can interrupt the live turn
        await runner.run(self._prompt, resume=self._resume, bypass=self._bypass,
                         model=self._model, effort=self._effort)


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


class AssistantPage(QWidget):
    _voice_routed = Signal(object, str)   # (SessionInfo|None, task) from the routing thread -> UI

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._thread: QThread | None = None
        self._worker: AgentWorker | None = None
        self._recorder = None        # StreamingTranscriber while dictating a task
        self._recording = False
        self._overlay = None         # ambient notch (lazy; see _ensure_notch)
        self._awaiting_approval = False
        self._resume_target = None   # {id, cwd, title} when continuing an existing conversation
        self._all_sessions = []      # cached list for in-memory search filtering
        self._loader_thread = None
        # Voice follow-up: while a conversation is "live" in the notch, the next thing you say
        # CONTINUES it (resume) instead of starting a new one — like VoiceOS.
        self._current_session = None   # {"id", "cwd"} of the active conversation
        self._current_cwd = None
        self._notch_active = False     # notch is showing a live conversation (not dismissed)
        self._pending_followup = None  # a follow-up spoken while a task was still running
        self._stopping = False         # user pressed Stop — suppress follow-up, show "Stopped"
        try:                           # model + reasoning effort (picked on the notch card, persisted)
            from app.core.settings import settings as _s
            self._model = _s.assistant_model or "opus"      # a real model is always selected (no Auto)
            self._effort = _s.assistant_effort or "high"
        except Exception:
            self._model, self._effort = "opus", "high"
        self._build()
        self._load_sessions()
        self._voice_routed.connect(self._on_voice_routed)

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
        try:
            ov.set_model_effort(self._model, self._effort)   # reflect the saved choice on the pill
        except Exception:
            pass
        self._overlay = ov
        return ov

    def _on_notch_dismissed(self):
        """✕ on the notch ends the active conversation — the next voice command starts fresh."""
        self._notch_active = False
        self._current_session = None

    def _on_stop_task(self):
        """Stop on ONE click: interrupt the turn now, show immediate feedback, and force-cancel as a
        fallback if the interrupt didn't end it."""
        if self._thread is None or self._worker is None:
            return
        self._stopping = True
        self._worker.stop()                 # interrupt the live turn (responsive)
        self._notch("mark_stopping")        # disable the Stop button + show "Stopping…"
        self.status_label.setText("Stopping…")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2500, self._force_stop)

    def _force_stop(self):
        if self._thread is not None and self._worker is not None:
            self._worker.cancel()           # forceful: cancel the task -> closes client -> kills subprocess

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
        sid = getattr(self, "_last_session_id", "") or ""
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
        self._loader.loaded.connect(self._loader_thread.quit)
        self._loader_thread.start()

    def _on_sessions_loaded(self, sessions):
        self._all_sessions = sessions or []
        self._populate_list(self._all_sessions)

    def _populate_list(self, sessions):
        self.session_list.clear()
        c = theme_manager.get_colors()
        active = (self._current_session or {}).get("id")
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
        self._resume_target = {"id": sid, "cwd": cwd, "title": title}
        self.resume_label.setText(f"↻  Continuing:  {title}")
        self.resume_banner.setVisible(True)
        self.task_input.setFocus()

    def _clear_resume(self):
        self._resume_target = None
        self._notch_active = False       # end the live conversation — next voice starts fresh
        self._current_session = None
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

    def run_voice_task(self, text: str):
        """Fn+Control entry. Route the spoken command to a NEW or RESUMED conversation via one
        quick LLM call (off the UI thread), then run it. The notch shows "Figuring out which
        conversation…" meanwhile, then names the one it picked. Falls back to a new conversation
        on any routing failure, so speech always does something."""
        text = (text or "").strip()
        if not text:
            return
        # A task is still running — treat this as a follow-up and send it when the turn finishes.
        if self._thread is not None:
            self._pending_followup = text
            self._notch("show_working", "Queued — will continue when this step finishes")
            self._notch("flash")
            return
        # We're in a LIVE conversation (notch up, not dismissed) — continue it, don't start new.
        if self._notch_active and self._current_session:
            self._notch("present")
            self._notch("set_title", "Following up…")
            self._notch("flash")
            self.run_task(text, resume=self._current_session["id"], cwd=self._current_session["cwd"])
            return
        # Fresh: route to a new conversation (or a named existing one), as before.
        self._notch("present")
        self._notch("set_title", "Finding the right conversation…")
        self._notch("show_working", "", "")
        self._notch("flash")
        import threading

        def worker():
            try:
                from app.core.agent_sessions import route_speech
                r = route_speech(text)
            except Exception:
                r = {"session": None, "task": text}
            self._voice_routed.emit(r.get("session"), r.get("task") or text)
        threading.Thread(target=worker, daemon=True, name="filect-assistant-route").start()

    def _on_voice_routed(self, session, task):
        """Routing finished (UI thread): resume the matched conversation, or start a new one."""
        if session is not None:
            self._resume_target = {"id": session.id, "cwd": session.cwd, "title": session.title}
            self.resume_label.setText(f"↻  Continuing:  {session.title}")
            self.resume_banner.setVisible(True)
            self._notch("set_title", f"Continuing “{session.title}”")
            self.run_task(task, resume=session.id, cwd=session.cwd)
        else:
            self._resume_target = None   # honour the LLM's "new" decision over any stale selection
            self.resume_banner.setVisible(False)
            self.run_task(task)

    def _run(self):
        self.run_task(self.task_input.toPlainText(), self.bypass_check.isChecked())

    def run_task(self, prompt: str, bypass: bool | None = None,
                 resume: str | None = None, cwd: str | None = None):
        """Start (or RESUME) an Assistant task. Shared by the Run button, the Fn+Control voice
        trigger, and (soon) the session browser. resume=<session_id> + cwd=<that session's folder>
        continues an existing local Claude Code conversation with its full prior context."""
        prompt = (prompt or "").strip()
        if not prompt:
            return
        if self._thread is not None:
            # Already running a task — show its status instead of silently doing nothing
            # (so a 2nd voice trigger isn't a mystery no-op).
            self._notch("flash")
            return
        if bypass is None:
            bypass = self.bypass_check.isChecked()
        # A conversation selected in the sidebar is the active context until you hit "New".
        if resume is None and self._resume_target:
            resume = self._resume_target["id"]
            cwd = cwd or self._resume_target["cwd"]
        import os
        workdir = cwd or DEFAULT_WORKSPACE
        if cwd is None:
            os.makedirs(workdir, exist_ok=True)   # only auto-create our OWN default workspace
        self._current_cwd = workdir
        self._notch_active = True                 # a conversation is now live (for voice follow-up)
        if self.task_input.toPlainText().strip() != prompt:
            self.task_input.setPlainText(prompt)   # reflect a voice task in the box
        self._current_prompt = prompt              # shown on the notch's done card
        self._result_text = ""                     # accumulates what Claude generates (for the card)
        self.feed.clear()
        if resume:
            self._append(f"↻ continuing session {resume[:8]}…\n")
        self._append(f"▶ {prompt}\n")
        if bypass:
            self._append("⚡ Bypass mode: running without approval prompts.\n")
        self.run_btn.setEnabled(False)
        self.status_label.setText("Running…")
        self._notch("present")        # drop the ambient handle under the notch
        self._notch("set_title", prompt)
        self._notch("show_working", "Starting…", "")
        self._notch("flash")          # brief 'receipt' so a voice trigger is visibly confirmed

        self._thread = QThread(self)
        self._worker = AgentWorker(workdir, prompt, resume=resume, bypass=bypass,
                                   model=(self._model or None), effort=(self._effort or None))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.step.connect(self._on_step)
        self._worker.approvalRequested.connect(self._on_approval)
        self._worker.result.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.done.connect(self._on_done)
        self._thread.start()

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

    def _on_step(self, step):
        if step.kind == "tool":
            label = self._tool_label(step.tool, getattr(step, "detail", ""))
            self._append(f"  → {label}")
            self._notch("show_working", label, "")    # readable action, also into the Details feed
        elif step.kind == "text" and step.text:
            self._append(step.text)
            self._result_text = (getattr(self, "_result_text", "") + " " + step.text).strip()
            self._notch("show_working", step.text, "")

    def _on_approval(self, req):
        self._awaiting_approval = True
        self.card_title.setText(f"The assistant wants to use:  {req.tool}")
        detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in (req.tool_input or {}).items())
        self.card_detail.setText(detail or "(no details)")
        self.card.setVisible(True)
        self._notch("show_needs_you", req.tool, req.tool_input)

    def _decide(self, approved: bool):
        # Either surface (in-tab card OR the notch) can answer; honour only the first.
        if not self._awaiting_approval:
            return
        self._awaiting_approval = False
        self.card.setVisible(False)
        self._append(f"  {'✓ approved' if approved else '✗ denied'}")
        if self._worker:
            self._worker.resolve(approved)

    def _on_result(self, session_id: str, cost: float):
        self._last_session_id = session_id or ""   # for "View task →" deep link into Claude desktop
        if session_id:                              # remember it so a voice follow-up continues it
            self._current_session = {"id": session_id, "cwd": self._current_cwd or DEFAULT_WORKSPACE}
        self.status_label.setText(f"Done — saved as session {session_id[:8]}…  (${cost:.2f})" if session_id
                                  else f"Done (${cost:.2f})")
        self._notch("show_done", session_id, cost, getattr(self, "_current_prompt", ""),
                    getattr(self, "_result_text", ""))

    def _on_error(self, msg: str):
        self._append(f"\n⚠ error: {msg}")
        self.status_label.setText("Error")
        self._notch("show_error", msg)

    def _on_done(self):
        self._awaiting_approval = False
        self.card.setVisible(False)
        self.run_btn.setEnabled(True)
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
        if self._stopping:                      # user hit Stop — don't fire any queued follow-up
            self._stopping = False
            self._pending_followup = None
            self.status_label.setText("Stopped")
            self._notch("show_done", "", 0.0, "", "Stopped by you.")
            return
        # A follow-up was spoken while this was running — send it now, continuing the same chat.
        if self._pending_followup and self._current_session:
            text, self._pending_followup = self._pending_followup, None
            self.run_task(text, resume=self._current_session["id"], cwd=self._current_session["cwd"])
        else:
            self._pending_followup = None
