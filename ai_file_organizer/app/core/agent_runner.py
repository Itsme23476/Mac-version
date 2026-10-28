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

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Set

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

    async def _can_use_tool(self, tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if tool_name in self.auto_allow:
            return PermissionResultAllow()
        approved = await self.on_approval(ApprovalRequest(tool_name, dict(tool_input or {})))
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user")

    async def run(self, prompt: str, resume: Optional[str] = None, max_turns: int = 40) -> Optional[str]:
        """Run (or resume) a task to completion, streaming steps and gating tools. Returns the
        session id (for resuming later)."""
        from claude_agent_sdk import (
            ClaudeSDKClient, ClaudeAgentOptions,
            AssistantMessage, TextBlock, ToolUseBlock, ResultMessage,
        )
        opts = ClaudeAgentOptions(
            cwd=self.cwd,
            can_use_tool=self._can_use_tool,
            permission_mode="default",
            setting_sources=[],     # our Approve/Deny cards are the gate, not the user's global ~/.claude settings
            resume=resume,          # continue an existing (e.g. Claude Desktop) session when given
            max_turns=max_turns,
        )
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            self.on_step(Step("text", text=b.text.strip()))
                        elif isinstance(b, ToolUseBlock):
                            self.on_step(Step("tool", tool=b.name))
                elif isinstance(msg, ResultMessage):
                    self.session_id = getattr(msg, "session_id", None)
                    self.on_result(self.session_id, getattr(msg, "total_cost_usd", 0.0) or 0.0)
        return self.session_id


# ── Qt layer (to build next) ─────────────────────────────────────────────────────────────────
# A thin QObject wrapper runs AgentRunner.run(...) in an asyncio loop on a worker thread and:
#   • on_step / on_result  -> emit Qt signals (queued to the UI thread) that update the task tile
#   • on_approval          -> create a pending asyncio.Future, emit a signal to show the Approve/Deny
#                             card, and await that Future; the card's buttons resolve it (True/False).
# To list/pick existing sessions for resume, use claude_agent_sdk.list_sessions(); the saved sessions
# are the same ones Claude Desktop shows for that project directory.
