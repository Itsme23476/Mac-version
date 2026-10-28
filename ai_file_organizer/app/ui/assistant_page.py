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
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton, QFrame, QScrollArea,
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

    def __init__(self, cwd: str, prompt: str, resume: str | None = None):
        super().__init__()
        self._cwd = cwd
        self._prompt = prompt
        self._resume = resume
        self._loop: asyncio.AbstractEventLoop | None = None
        self._approval_fut: asyncio.Future | None = None

    def run(self):  # executes on the worker thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._go())
        except Exception as e:  # surface any failure instead of dying silently
            self.error.emit(str(e))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.done.emit()

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
        await runner.run(self._prompt, resume=self._resume)


class AssistantPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._thread: QThread | None = None
        self._worker: AgentWorker | None = None
        self._build()

    # ---- UI ----
    def _build(self):
        c = theme_manager.get_colors()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        title = QLabel("Assistant")
        title.setStyleSheet("font-size: 22px; font-weight: 700; background: transparent; border: none;")
        root.addWidget(title)
        sub = QLabel("Describe a task and a local Claude agent will do it — you approve each action. "
                     "Every task is saved and can be continued in Claude Desktop.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #7A7A90; font-size: 13px; background: transparent; border: none;")
        root.addWidget(sub)

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
        self.run_btn = QPushButton("Run")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.setStyleSheet(
            "QPushButton { background:#7C4DFF; color:white; border:none; border-radius:9px; "
            "padding:0 22px; font-weight:600; } QPushButton:hover { background:#6A3DF0; } "
            "QPushButton:disabled { background:#B9A8F0; }")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)
        root.addLayout(row)

        # Approval card (hidden until the agent asks)
        self.card = QFrame()
        self.card.setObjectName("approvalCard")
        self.card.setStyleSheet(
            f"QFrame#approvalCard {{ background:{c['surface']}; border:1px solid #7C4DFF; border-radius:12px; }}"
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

    # ---- actions ----
    def _append(self, text: str):
        self.feed.append(text)

    def _run(self):
        prompt = self.task_input.toPlainText().strip()
        if not prompt or self._thread is not None:
            return
        import os
        os.makedirs(DEFAULT_WORKSPACE, exist_ok=True)
        self.feed.clear()
        self._append(f"▶ {prompt}\n")
        self.run_btn.setEnabled(False)
        self.status_label.setText("Running…")

        self._thread = QThread(self)
        self._worker = AgentWorker(DEFAULT_WORKSPACE, prompt)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.step.connect(self._on_step)
        self._worker.approvalRequested.connect(self._on_approval)
        self._worker.result.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.done.connect(self._on_done)
        self._thread.start()

    def _on_step(self, step):
        if step.kind == "tool":
            self._append(f"  → used {step.tool}")
        elif step.kind == "text" and step.text:
            self._append(step.text)

    def _on_approval(self, req):
        self.card_title.setText(f"The assistant wants to use:  {req.tool}")
        detail = ", ".join(f"{k}={str(v)[:80]}" for k, v in (req.tool_input or {}).items())
        self.card_detail.setText(detail or "(no details)")
        self.card.setVisible(True)

    def _decide(self, approved: bool):
        self.card.setVisible(False)
        self._append(f"  {'✓ approved' if approved else '✗ denied'}")
        if self._worker:
            self._worker.resolve(approved)

    def _on_result(self, session_id: str, cost: float):
        self.status_label.setText(f"Done — saved as session {session_id[:8]}…  (${cost:.2f})" if session_id
                                  else f"Done (${cost:.2f})")

    def _on_error(self, msg: str):
        self._append(f"\n⚠ error: {msg}")
        self.status_label.setText("Error")

    def _on_done(self):
        self.card.setVisible(False)
        self.run_btn.setEnabled(True)
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
