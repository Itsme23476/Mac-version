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

_login_path_done = False


def use_login_path():
    """Adopt the user's login-shell PATH, once, before launching `claude`. A Finder-launched app
    only gets PATH=/usr/bin:/bin:/usr/sbin:/sbin: the SDK then picked an old npm `claude`
    (`#!/usr/bin/env node`) that can't find node → exit 127 → every task failed, and the agent's own
    commands couldn't see Homebrew/npm tools. With the login PATH it uses the same `claude` (and
    tools) Terminal would."""
    global _login_path_done
    if _login_path_done:
        return
    _login_path_done = True
    import pwd
    import subprocess
    home = os.path.expanduser("~")
    entries = []
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
        out = subprocess.run([shell, "-ilc", 'printf "\\n__FILECT_PATH__%s\\n" "$PATH"'],
                             capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL).stdout
        line = next((l for l in out.splitlines() if l.startswith("__FILECT_PATH__")), "")
        entries = line[len("__FILECT_PATH__"):].split(":") if line else []
    except Exception as e:
        logger.warning(f"[agent] login PATH unavailable ({e}); using common locations")
    # where `claude` / Homebrew / npm usually live, in case the shell gave us nothing
    entries += [f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(":")
    merged = list(dict.fromkeys(p for p in entries + current if p))
    os.environ["PATH"] = ":".join(merged)
    logger.info(f"[agent] PATH for claude: {len(merged)} dirs (login shell gave {len(entries) - 3})")


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


def wait_for_file(path: str, timeout: float = 5.0) -> bool:
    """screen_ink writes a screenshot's PNG in the background; it appears atomically when complete."""
    import time
    deadline = time.time() + timeout
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


def _image_block(path: str) -> Optional[dict]:
    """A screenshot as an image content block for Claude: downscaled to 1568px on the long edge
    (Claude's sweet spot; full resolution stays on disk) and PNG-encoded so red marks stay crisp —
    JPEG only if the PNG is still too big for the API's per-image limit."""
    import base64
    try:
        from PySide6.QtCore import QBuffer, QIODevice, Qt
        from PySide6.QtGui import QImage
    except Exception:
        return None
    wait_for_file(path)
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
    if len(imgs) == 1:
        blocks = imgs
    else:
        # several (receive mode): label each image so "[screenshot 2]" in my words points at it
        blocks = [x for i, b in enumerate(imgs, 1) for x in ({"type": "text", "text": f"[screenshot {i}]"}, b)]
    text = screenshot_text(prompt, images, len(imgs))
    return {"type": "user", "message": {"role": "user", "content": blocks + [{"type": "text", "text": text}]},
            "parent_tool_use_id": None}


def screenshot_text(prompt: str, images, n: int) -> str:
    """Your words + what the n attached screenshots are and where each is saved (shared with Codex)."""
    if n == 1:
        note = "Screenshot of my screen attached"
    else:
        note = (f"{n} screenshots of my screen attached, in order — [screenshot N] in my words marks "
                "where I took each")
    note += " — anything marked in red is what I'm pointing at."
    return f"{prompt}\n\n({note})\n" + "\n".join(f"[screenshot: {p}]" for p in images)


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


# Setup problems in plain words. They are not task failures: the card shows them with a one-click fix
# (open_claude_setup) instead of a raw "Claude Code not found at: …" or a fake "Done: Not logged in".
SETUP_INSTALL = ("Claude Code isn't installed on this Mac yet. The Assistant runs on Claude Code with "
                 "your Claude plan. Set it up once (about a minute), then try again.")
SETUP_SIGNIN = "Claude Code isn't signed in yet. Sign in once with your Claude account, then try again."
CODEX_SETUP_INSTALL = ("Codex isn't installed on this Mac yet. The Assistant can run on Codex with your "
                       "ChatGPT plan. Set it up once (about a minute), then try again.")
CODEX_SETUP_SIGNIN = "Codex isn't signed in yet. Sign in once with your ChatGPT account, then try again."
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
_SETUP = {("claude", "install"): SETUP_INSTALL, ("claude", "signin"): SETUP_SIGNIN,
          ("codex", "install"): CODEX_SETUP_INSTALL, ("codex", "signin"): CODEX_SETUP_SIGNIN}


def usage_message(agent: str, detail: str = "") -> str:
    """Out of usage on the plan — the agent's own words carry when it resets."""
    return f"{AGENT_NAMES[agent]} is out of usage for now. {(detail or '').strip()}".strip()


class AgentSetupError(Exception):
    """The agent can't run right now: missing ("install"), signed out ("signin") or out of usage on the
    plan ("usage"). str() is the friendly message — the Assistant switches to the other agent."""
    def __init__(self, kind: str, agent: str = "claude", detail: str = ""):
        self.kind, self.agent = kind, agent
        super().__init__(usage_message(agent, detail) if kind == "usage" else _SETUP[(agent, kind)])


def setup_info(message: str):
    """(agent, kind) for an AgentSetupError's message, else None (a real task error)."""
    m = (message or "").strip()
    for key, text in _SETUP.items():
        if m == text:
            return key
    for agent, name in AGENT_NAMES.items():
        if m.startswith(f"{name} is out of usage"):
            return agent, "usage"
    return None


def setup_kind(message: str) -> str:
    """"install" / "signin" / "usage" for a setup message, else "" (a real task error)."""
    info = setup_info(message)
    return info[1] if info else ""


def open_claude_setup(kind: str) -> str:
    """Open Terminal running Anthropic's OFFICIAL installer (only if it's missing), then `claude auth login`
    to sign in with your Claude account, then a plain "all set" — every step visible. Returns the path."""
    import subprocess
    import tempfile
    lines = ["#!/bin/bash", "clear", 'echo "Filect · setting up Claude Code"', "echo"]
    if kind == "install":
        lines += ['echo "Installing Claude Code with Anthropic\'s official installer (claude.ai/install.sh)…"',
                  "curl -fsSL https://claude.ai/install.sh | bash || "
                  '{ echo; echo "The install did not finish. See above."; read -n 1; exit 1; }', "echo"]
    # `claude auth login` signs in and then EXITS (plain `claude` took over the window, wiped these
    # instructions and left people inside Claude Code). Then confirm it worked, in plain words.
    lines += ['export PATH="$HOME/.local/bin:$PATH"',
              'echo "Signing in with your Claude account. A browser window will open."', "echo",
              "claude auth login --claudeai", "echo",
              "if claude auth status 2>/dev/null | grep -q '\"loggedIn\": true'; then",
              '  echo "✓ All set. Go back to Filect and try again. You can close this window."',
              "else",
              '  echo "Sign-in didn\'t finish. Click the button in Filect to try again."',
              "fi"]
    path = os.path.join(tempfile.gettempdir(), "filect-claude-setup.command")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    subprocess.Popen(["open", "-a", "Terminal", path])
    logger.info("[agent] opened Claude Code setup (%s)", kind)
    return path


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
        from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions, ResultMessage,
                                      AssistantMessage, CLINotFoundError)
        use_login_path()
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
        try:
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
                    err = getattr(msg, "error", None) if isinstance(msg, AssistantMessage) else None
                    if err == "authentication_failed":
                        # Signed out: Claude Code answers "Not logged in · Please run /login" as if it were
                        # the reply — that showed as a DONE card. It's a setup step, not an answer.
                        raise AgentSetupError("signin")
                    if err in ("rate_limit", "billing_error"):
                        # Out of usage ("You've hit your limit · resets 3pm") — also not an answer: the
                        # Assistant hands the task to the other agent.
                        from claude_agent_sdk import TextBlock
                        said = " ".join(b.text for b in msg.content if isinstance(b, TextBlock))
                        raise AgentSetupError("usage", detail=said)
                    for st in _steps_from_message(msg):
                        self.on_step(st)
                    if isinstance(msg, ResultMessage):
                        self.session_id = getattr(msg, "session_id", None)
                        self.on_result(self.session_id, getattr(msg, "total_cost_usd", 0.0) or 0.0)
        except CLINotFoundError as e:            # no `claude` anywhere (the packaged app doesn't bundle it)
            raise AgentSetupError("install") from e
        finally:
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
