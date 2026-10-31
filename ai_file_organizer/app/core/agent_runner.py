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
    kind: str            # "text" | "tool"
    text: str = ""       # assistant text (kind="text")
    tool: str = ""       # tool name (kind="tool")
    detail: str = ""     # the tool's key target — file path, command, url — for a readable line


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
                  effort: Optional[str] = None) -> Optional[str]:
        """Run (or resume) a task to completion, streaming steps and gating tools. Returns the
        session id (for resuming later).

        bypass=True runs without any Approve/Deny cards — like Claude Code's bypass-permissions
        mode, the agent edits files and runs commands on its own. Use only for trusted tasks.
        """
        from claude_agent_sdk import (
            ClaudeSDKClient, ClaudeAgentOptions,
            AssistantMessage, TextBlock, ToolUseBlock, ResultMessage,
        )
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
        async with ClaudeSDKClient(options=opts) as client:
            self._client = client            # exposed so stop() can interrupt the running turn
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            self.on_step(Step("text", text=b.text.strip()))
                        elif isinstance(b, ToolUseBlock):
                            self.on_step(Step("tool", tool=b.name,
                                              detail=_tool_detail(b.name, getattr(b, "input", None))))
                elif isinstance(msg, ResultMessage):
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
