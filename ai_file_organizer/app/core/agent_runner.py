"""Drive a local Claude Code agent as a saved, resumable session — with live steps and a
human Approve/Deny on each risky tool use.

This is the engine behind Filect's "Agents" feature. It is deliberately UI-agnostic: the caller
passes three callbacks and the Qt layer adapts them to signals + an approval card. Each task is a
real Claude Code session stored under ~/.claude/projects (the SAME store Claude Desktop reads), so
tasks are automatically saved, listed, and resumable there — pass `resume=<session_id>` to continue
an existing chat.

Dependency: `claude-agent-sdk` (wraps the local `claude` CLI; uses the user's Claude Code login).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Set

logger = logging.getLogger(__name__)

# Read-only / safe tools run without interrupting the user. Everything else (Write, Edit, Bash,
# deletes, network side effects, MCP tools) raises an Approve/Deny card. Tunable per the product's
# safety posture. ponytail: allow-list the safe ones, gate the rest — simplest rule that's safe.
DEFAULT_AUTO_ALLOW: Set[str] = {
    "Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "WebFetch", "WebSearch",
}


@dataclass
class Step:
    """One live update to show in the task's tile."""
    kind: str            # "text" | "tool" | "result"
    text: str = ""       # assistant text (kind="text") / the tool's output (kind="result")
    tool: str = ""       # tool name (kind="tool")
    detail: str = ""     # the tool's key target — file path, command, url — for a readable line
    input: dict = field(default_factory=dict)   # the tool call's full input (file content, edit, todos…)
    tool_id: str = ""    # pairs a "result" with its "tool" step
    is_error: bool = False   # kind="result": the tool failed


def _tool_detail(name: str, inp: dict) -> str:
    """A short human-readable target for a tool call, so the feed reads 'Writing index.html'
    instead of 'Using Write'. (The full inputs/outputs live in the real Claude Code conversation.)"""
    inp = inp or {}
    if name in ("Write", "Edit", "MultiEdit"):
        return os.path.basename(str(inp.get("file_path", "")))
    if name == "NotebookEdit":
        return os.path.basename(str(inp.get("notebook_path", "")))
    if name == "Bash":
        return str(inp.get("command", ""))[:90]
    if name in ("Read", "Glob"):
        return os.path.basename(str(inp.get("file_path", "") or inp.get("pattern", "")))
    if name == "Grep":
        return str(inp.get("pattern", ""))[:60]
    if name == "WebFetch":
        return str(inp.get("url", ""))[:70]
    if name in ("Task", "TodoWrite"):
        return str(inp.get("description", ""))[:60]
    return ""


def _image_block(path: str) -> Optional[dict]:
    """A screenshot as an image content block for Claude: downscaled to 1568px on the long edge
    (Claude's sweet spot; full resolution stays on disk) and PNG-encoded so red marks stay crisp —
    JPEG only if the PNG is still too big for the API's per-image limit."""
    import base64
    import time
    try:
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
    except Exception:
        return None
    deadline = time.time() + 5               # screen_ink writes the PNG in the background; it appears
    while not os.path.exists(path) and time.time() < deadline:     # atomically when complete
        time.sleep(0.05)
    img = QImage(path)                       # QImage (not QPixmap) is fine off the UI thread
    if img.isNull():
        logger.warning("[agent] screenshot unreadable: %s", path)
        return None
    if max(img.width(), img.height()) > 1568:
        img = img.scaled(1568, 1568, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _enc(fmt, *q):
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, fmt, *q)
        return bytes(buf.data())

    data, media = _enc("PNG"), "image/png"
    if len(data) > 3_500_000:
        data, media = _enc("JPEG", 85), "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(data).decode()}}


def user_message(prompt: str, images) -> dict:
    """The user turn for a task with screenshots: the images, then your words + a note saying what they
    are and where each is saved ("[screenshot: …]" lines — Claude can reuse the file, and the notch
    shows them as thumbnails when the chat is reopened)."""
    imgs = [b for b in (_image_block(p) for p in images) if b]
    n = len(imgs)
    if n == 1:
        blocks, note = imgs, "Screenshot of my screen attached"
    else:
        # several (receive mode): label each image so "[screenshot 2]" in my words points at it
        blocks = [x for i, b in enumerate(imgs, 1) for x in ({"type": "text", "text": f"[screenshot {i}]"}, b)]
        note = (f"{n} screenshots of my screen attached, in order — [screenshot N] in my words marks "
                "where I took each")
    note += " — anything marked in red is what I'm pointing at."
    text = f"{prompt}\n\n({note})\n" + "\n".join(f"[screenshot: {p}]" for p in images)
    return {"type": "user", "message": {"role": "user", "content": blocks + [{"type": "text", "text": text}]},
            "parent_tool_use_id": None}


def _steps_from_message(msg) -> list:
    """Steps for one SDK message: Claude's text + tool calls (AssistantMessage), and the tool results
    the engine feeds back (UserMessage) — so the notch can show whether a write/command worked and what
    it printed. Thinking and the echoed prompt are skipped."""
    from claude_agent_sdk import AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock
    from app.core.agent_sessions import tool_result_text
    out = []
    if isinstance(msg, AssistantMessage):
        for b in msg.content:
            if isinstance(b, TextBlock) and b.text.strip():
                out.append(Step("text", text=b.text.strip()))
            elif isinstance(b, ToolUseBlock):
                inp = getattr(b, "input", None) or {}
                out.append(Step("tool", tool=b.name, detail=_tool_detail(b.name, inp), input=inp, tool_id=b.id))
    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
        for b in msg.content:
            if isinstance(b, ToolResultBlock):
                out.append(Step("result", tool_id=b.tool_use_id, text=tool_result_text(b.content),
                                is_error=bool(b.is_error)))
    return out


@dataclass
class ApprovalRequest:
    """What the agent wants to do — rendered as the "needs you" card."""
    tool: str
    tool_input: dict = field(default_factory=dict)


class AgentRunner:
    """One task == one Claude session.

    Callbacks (all provided by the caller / Qt layer):
      on_step(Step)                         -> live progress into the tile
      on_approval(ApprovalRequest) -> Awaitable[bool]   True=approve, False=deny (await the user's tap)
      on_result(session_id, cost_usd)       -> task finished

    The session id is captured on finish so the task can be resumed later (here or in Claude Desktop).
    """

    def __init__(
        self,
        cwd: str,
        on_step: Callable[[Step], None],
        on_approval: Callable[[ApprovalRequest], "Awaitable[bool]"],
        on_result: Callable[[Optional[str], float], None],
        auto_allow: Optional[Set[str]] = None,
    ):
        self.cwd = cwd
        self.on_step = on_step
        self.on_approval = on_approval
        self.on_result = on_result
        self.auto_allow = DEFAULT_AUTO_ALLOW if auto_allow is None else auto_allow
        self.session_id: Optional[str] = None
        self._client = None          # live ClaudeSDKClient (set during run) — for interrupt()

    async def _can_use_tool(self, tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if tool_name in self.auto_allow:
            return PermissionResultAllow()
        approved = await self.on_approval(ApprovalRequest(tool_name, dict(tool_input or {})))
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user")

    async def run(self, prompt: str, resume: Optional[str] = None, max_turns: int = 40,
                  bypass: bool = False, model: Optional[str] = None,
                  effort: Optional[str] = None, images: Optional[list] = None) -> Optional[str]:
        """Run (or resume) a task to completion, streaming steps and gating tools. Returns the
        session id (for resuming later).

        bypass=True runs without any Approve/Deny cards — like Claude Code's bypass-permissions
        mode, the agent edits files and runs commands on its own. Use only for trusted tasks.
        """
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage
        # setting_sources=[] isolates from the user's global ~/.claude permissions in both modes.
        common = dict(cwd=self.cwd, setting_sources=[], resume=resume, max_turns=max_turns)
        if model:            # '' / None -> let Claude Code use its default model
            common["model"] = model
        if effort:           # '' / None -> default reasoning effort
            common["effort"] = effort
        if bypass:
            # Engine skips permission prompts entirely — no callback, no cards.
            opts = ClaudeAgentOptions(permission_mode="bypassPermissions", **common)
        else:
            # Our Approve/Deny cards are the gate (read-only tools auto-allowed).
            opts = ClaudeAgentOptions(permission_mode="default", can_use_tool=self._can_use_tool, **common)
        # Proof (not a mockup): these become real `--model` / `--effort` CLI flags to Claude Code.
        logger.info("[agent] starting: model=%s effort=%s bypass=%s resume=%s",
                    model or "(default)", effort or "(default)", bypass, bool(resume))
        # fn + ⌃ + Space screenshots go in the message itself — built BEFORE connecting, since it may
        # briefly wait for a screenshot that's still being written.
        msg = user_message(prompt, images) if images else None
        async with ClaudeSDKClient(options=opts) as client:
            self._client = client            # exposed so stop() can interrupt the running turn
            if msg is not None:
                logger.info("[agent] sending %d screenshot(s)",
                            sum(b["type"] == "image" for b in msg["message"]["content"]))

                async def _one():
                    yield msg
                await client.query(_one())
            else:
                await client.query(prompt)
            async for msg in client.receive_response():
                for st in _steps_from_message(msg):
                    self.on_step(st)
                if isinstance(msg, ResultMessage):
                    self.session_id = getattr(msg, "session_id", None)
                    self.on_result(self.session_id, getattr(msg, "total_cost_usd", 0.0) or 0.0)
        self._client = None
        return self.session_id

    async def interrupt(self) -> None:
        """Stop the running turn now (the SDK-sanctioned stop). Ends receive_response() so the task
        finishes promptly — far more responsive than cancelling the asyncio task."""
        c = self._client
        if c is None:
            return
        try:
            await c.interrupt()
            logger.info("[agent] interrupt sent")
        except Exception as e:
            logger.warning("[agent] interrupt failed: %s", e)


# ── Qt layer (to build next) ─────────────────────────────────────────────────────────────────
# A thin QObject wrapper runs AgentRunner.run(...) in an asyncio loop on a worker thread and:
#   • on_step / on_result  -> emit Qt signals (queued to the UI thread) that update the task tile
#   • on_approval          -> create a pending asyncio.Future, emit a signal to show the Approve/Deny
#                             card, and await that Future; the card's buttons resolve it (True/False).
# To list/pick existing sessions for resume, use claude_agent_sdk.list_sessions(); the saved sessions
# are the same ones Claude Desktop shows for that project directory.
