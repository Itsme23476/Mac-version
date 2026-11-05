"""Drive a local Codex CLI agent (OpenAI) — the Assistant's second engine, next to Claude Code.

Same contract as AgentRunner (on_step(Step) live, on_result(session_id, cost) at the end), so the notch,
rail and Details render a Codex chat exactly like a Claude one. `codex exec --json` streams JSONL events
(thread.started → item.started/completed → turn.completed | turn.failed); each maps to the Step the UI
already knows. Every task is a real, saved Codex session (~/.codex/sessions), continued with
`codex exec resume <id>`.

No Approve/Deny with Codex (exec never asks): Bypass = full access, otherwise the workspace-write
sandbox — edits inside the task folder only, no network.

Verified on codex-cli 0.153.3: event shapes, `--` before the prompt (so `-i` can't swallow it),
`-c sandbox_mode=…` (read-only really blocks writes), `--ignore-user-config` keeps the ChatGPT login
(auth.json), `codex login status` exit 0/1, a signed-out run retrying 401s ~30s before failing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from app.core.agent_runner import (AgentSetupError, Step, _tool_detail, screenshot_text, use_login_path,
                                   wait_for_file)

logger = logging.getLogger(__name__)

# Used only if Codex's own model list (models_cache.json) can't be read. Checked on this Mac 2026-11-05.
_FALLBACK_MODELS = [("gpt-6-astra", "GPT-6-Astra"), ("gpt-5.6-sol", "GPT-5.6-Sol"),
                    ("gpt-5.6-terra", "GPT-5.6-Terra"), ("gpt-5.6-luna", "GPT-5.6-Luna")]
# Codex's own wording for an exhausted plan (from the CLI binary): "You've hit your usage limit…",
# "Usage limit reached…", "You're out of credits…", "Quota exceeded…", "…spend cap…".
_USAGE_RE = re.compile(r"usage limit|out of credits|quota exceeded|spend cap", re.I)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def find_codex() -> Optional[str]:
    use_login_path()                    # adds ~/.local/bin (the official installer's spot) + Homebrew
    return shutil.which("codex")


def signed_in(exe: str) -> bool:
    """`codex login status`: exit 0 = signed in (~20ms). A signed-out `exec` instead retries 401s ~30s."""
    try:
        return subprocess.run([exe, "login", "status"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def codex_models() -> list:
    """[(slug, display name)] Codex offers this account, best first — its own cached model list."""
    try:
        data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
        ms = sorted((m for m in data.get("models", []) if m.get("visibility") == "list" and m.get("slug")),
                    key=lambda m: m.get("priority", 99))
        out = [(m["slug"], m.get("display_name") or m["slug"]) for m in ms]
        if out:
            return out
    except Exception:
        pass
    return list(_FALLBACK_MODELS)


def _unavailable(msg: str) -> Optional[AgentSetupError]:
    """Codex's error text → out of usage / signed out (so the Assistant switches agents), else None."""
    if _USAGE_RE.search(msg or ""):
        m = re.search(r"try again (?:at|in) [^.()]+", msg, re.I)
        return AgentSetupError("usage", "codex", (m.group(0)[0].upper() + m.group(0)[1:] + ".") if m
                               else msg[:160])
    if "401" in (msg or "") and "nauthorized" in msg:
        return AgentSetupError("signin", "codex")
    return None


def _shell_cmd(cmd) -> str:
    """`/bin/zsh -lc 'npm test'` (or ["/bin/zsh", "-lc", "npm test"]) → `npm test`."""
    try:
        argv = cmd if isinstance(cmd, list) else shlex.split(cmd or "")
        if len(argv) >= 3 and argv[1] in ("-lc", "-c"):
            return argv[2]
    except Exception:
        pass
    return " ".join(cmd) if isinstance(cmd, list) else (cmd or "")


def _file_changes(item) -> list:
    """[(path, kind, content|None)] — the live event lists [{path, kind}], the saved session a
    {path: {type, content}} map."""
    ch = item.get("changes") or []
    if isinstance(ch, dict):
        return [(p, (v or {}).get("type", "update"), (v or {}).get("content")) for p, v in ch.items()]
    return [(c.get("path", ""), c.get("kind", "update"), None) for c in ch if isinstance(c, dict)]


def _read(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""


def _item_steps(item: dict, completed: bool, saved: bool = False) -> list:
    """Steps for one Codex item, in the shapes Claude's steps have (Bash / Write / WebSearch / TodoWrite),
    so details_view renders them unchanged. Reasoning and transport warnings are skipped."""
    t, iid = (item.get("type") or ""), item.get("id") or ""
    t = re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()          # saved sessions say "CommandExecution"
    if t == "agent_message" and completed:
        text = item.get("text")
        if text is None:                                     # saved: content [{type: Text, text}]
            text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        return [Step("text", text=text.strip())] if text and text.strip() else []
    if t == "command_execution":
        cmd = _shell_cmd(item.get("command"))
        inp = {"command": cmd}
        tool = Step("tool", tool="Bash", detail=_tool_detail("Bash", inp), input=inp, tool_id=iid)
        if not completed:
            return [tool]
        res = Step("result", tool_id=iid, text=item.get("aggregated_output") or item.get("stdout") or "",
                   is_error=item.get("exit_code") not in (0, None) or item.get("status") == "failed")
        return [tool, res] if saved else [res]               # live: the tool step came on item.started
    if t == "file_change" and completed:                     # one Write per file, with what's in it now
        out = []
        for i, (path, kind, content) in enumerate(_file_changes(item)):
            fid = f"{iid}:{i}"
            if kind == "delete":
                inp = {"command": f"rm {shlex.quote(path)}"}
                out.append(Step("tool", tool="Bash", detail=os.path.basename(path), input=inp, tool_id=fid))
            else:
                inp = {"file_path": path, "content": content if content is not None else _read(path)}
                out.append(Step("tool", tool="Write", detail=os.path.basename(path), input=inp, tool_id=fid))
            out.append(Step("result", tool_id=fid, is_error=item.get("status") == "failed"))
        return out
    if t == "web_search" and completed:
        q = item.get("query") or ""
        return [Step("tool", tool="WebSearch", detail=q[:60], input={"query": q}, tool_id=iid)]
    if t == "mcp_tool_call" and completed:
        name = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
        return [Step("tool", tool=name or "MCP tool", input=dict(item.get("arguments") or {}), tool_id=iid),
                Step("result", tool_id=iid, is_error=bool(item.get("error")) or item.get("status") == "failed")]
    if t == "todo_list" and completed:
        todos = [{"content": x.get("text", ""), "status": "completed" if x.get("completed") else "pending"}
                 for x in item.get("items") or []]
        return [Step("tool", tool="TodoWrite", input={"todos": todos}, tool_id=iid)]
    return []


class CodexRunner:
    """One task == one Codex session. Same callbacks as AgentRunner (on_approval is never called)."""

    def __init__(self, cwd: str, on_step: Callable[[Step], None], on_approval=None,
                 on_result: Callable[[Optional[str], float], None] = None, auto_allow=None):
        self.cwd = cwd
        self.on_step = on_step
        self.on_result = on_result or (lambda *a: None)
        self.session_id: Optional[str] = None
        self._proc = None
        self._interrupted = False

    async def run(self, prompt: str, resume: Optional[str] = None, max_turns: int = 40,
                  bypass: bool = False, model: Optional[str] = None, effort: Optional[str] = None,
                  images: Optional[list] = None) -> Optional[str]:
        exe = find_codex()
        if not exe:
            raise AgentSetupError("install", "codex")
        if not signed_in(exe):
            raise AgentSetupError("signin", "codex")
        shots = [p for p in (images or []) if wait_for_file(p)]
        text = screenshot_text(prompt, images, len(shots)) if images else prompt
        # --ignore-user-config: like Claude's setting_sources=[] — your Codex MCP servers / hooks / notify
        # don't run inside Filect's tasks. The ChatGPT login (auth.json) still applies.
        argv = [exe, "exec"] + (["resume"] if resume else []) + ["--json", "--skip-git-repo-check",
                                                                 "--ignore-user-config"]
        argv += (["--dangerously-bypass-approvals-and-sandbox"] if bypass
                 else ["-c", 'sandbox_mode="workspace-write"'])
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        for p in shots:
            argv += ["-i", p]
        argv += ["--"] + ([resume] if resume else []) + [text]
        logger.info("[codex] starting: model=%s effort=%s bypass=%s resume=%s screenshots=%d",
                    model or "(default)", effort or "(default)", bypass, bool(resume), len(shots))
        self.session_id = resume
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, limit=1 << 24)          # one command's output is one JSON line
        self._proc = proc
        tail: List[str] = []

        async def drain():                                   # stderr must be read or Codex blocks on it
            async for ln in proc.stderr:
                tail.append(ln.decode("utf-8", "replace").strip())
                del tail[:-15]
        drainer = asyncio.ensure_future(drain())
        finished, failure = False, ""
        try:
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "thread.started":
                    self.session_id = ev.get("thread_id") or self.session_id
                elif t in ("item.started", "item.completed"):
                    item = ev.get("item") or {}
                    if item.get("type") == "error":          # a transport warning ("Falling back from…")
                        err = _unavailable(item.get("message", ""))
                        if err:
                            raise err
                        continue
                    for st in _item_steps(item, t == "item.completed"):
                        self.on_step(st)
                elif t in ("error", "turn.failed"):
                    msg = ev.get("message") or (ev.get("error") or {}).get("message") or ""
                    err = None if self._interrupted else _unavailable(msg)
                    if err:
                        raise err                            # don't sit through Codex's 30s of retries
                    if t == "turn.failed":
                        failure = msg
                elif t == "turn.completed":
                    finished = True
                    self.on_result(self.session_id, 0.0)     # the ChatGPT plan isn't billed per task
            await proc.wait()
        finally:
            if proc.returncode is None:                      # cancelled / raised: don't leave it running
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            drainer.cancel()
            self._proc = None
        if not finished and not self._interrupted:
            raise RuntimeError(failure or next((l for l in reversed(tail) if l), "")
                               or f"Codex stopped unexpectedly (exit {proc.returncode}).")
        return self.session_id

    async def interrupt(self) -> None:
        """Stop the running turn: Ctrl-C, as in Terminal (the worker force-kills if it lingers)."""
        p = self._proc
        if p is not None and p.returncode is None:
            self._interrupted = True
            try:
                p.send_signal(signal.SIGINT)
                logger.info("[codex] interrupt sent")
            except Exception as e:
                logger.warning("[codex] interrupt failed: %s", e)


# ---- the one-click fixes on the card ------------------------------------------------------------
def _open_terminal(name: str, lines: list) -> str:
    import tempfile
    path = os.path.join(tempfile.gettempdir(), name)
    head = ["#!/bin/bash", 'export PATH="$HOME/.local/bin:$PATH"']
    if os.environ.get("CODEX_HOME"):             # a custom Codex profile: Terminal must use the same one
        head.append(f"export CODEX_HOME={shlex.quote(os.environ['CODEX_HOME'])}")
    with open(path, "w") as f:
        f.write("\n".join(head + lines) + "\n")
    os.chmod(path, 0o755)
    subprocess.Popen(["open", "-a", "Terminal", path])
    return path


def open_codex_setup(kind: str) -> str:
    """Terminal: OpenAI's OFFICIAL installer (only if missing; CODEX_NON_INTERACTIVE so it doesn't drop
    you into Codex), then `codex login` (ChatGPT sign-in in the browser, then it exits), then a plain
    "all set". Returns the script's path."""
    lines = ["clear", 'echo "Filect · setting up Codex"', "echo"]
    if kind == "install":
        lines += ['echo "Installing Codex with OpenAI\'s official installer (chatgpt.com/codex/install.sh)…"',
                  "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh || "
                  '{ echo; echo "The install did not finish. See above."; read -n 1; exit 1; }', "echo"]
    lines += ['echo "Signing in with your ChatGPT account. A browser window will open."', "echo",
              "codex login", "echo",
              "if codex login status >/dev/null 2>&1; then",
              '  echo "✓ All set. Go back to Filect and try again. You can close this window."',
              "else",
              '  echo "Sign-in didn\'t finish. Click the button in Filect to try again."',
              "fi"]
    logger.info("[codex] opened Codex setup (%s)", kind)
    return _open_terminal("filect-codex-setup.command", lines)


def open_in_codex(session_id: str, cwd: str) -> str:
    """"Open in Codex": continue this chat in Codex itself (its terminal app), in the chat's folder."""
    return _open_terminal("filect-open-codex.command",
                          [f"cd {shlex.quote(cwd or str(Path.home()))} || exit 1",
                           f"exec codex resume {shlex.quote(session_id)}"])


# ---- voice routing when Claude isn't available --------------------------------------------------
def complete(system: str, prompt: str, timeout: float = 60.0) -> str:
    """One quick answer from Codex (no tools, nothing saved): the intent router's fallback."""
    import tempfile
    exe = find_codex()
    if not exe or not signed_in(exe):
        raise RuntimeError("Codex unavailable")
    with tempfile.TemporaryDirectory() as d:
        out = subprocess.run(
            [exe, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
             "-c", 'sandbox_mode="read-only"', "-c", 'model_reasoning_effort="low"', "--",
             f"{system}\n\nDo not run any commands. Reply with the answer only.\n\n{prompt}"],
            cwd=d, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout).stdout
    texts = []
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        it = ev.get("item") or {}
        if ev.get("type") == "item.completed" and it.get("type") == "agent_message":
            texts.append(it.get("text") or "")
    if not texts:
        raise RuntimeError("Codex gave no answer")
    return texts[-1]


# ---- saved Codex chats (the rail's history) ----------------------------------------------------
def _session_files(days: int = 45) -> list:
    cutoff = time.time() - days * 86400
    out = []
    for f in (codex_home() / "sessions").glob("*/*/*/rollout-*.jsonl"):
        try:
            if f.stat().st_mtime >= cutoff:
                out.append(f)
        except Exception:
            continue
    return out


def _events(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            yield d.get("type"), d.get("payload") or {}


def _user_text(item: dict) -> str:
    return " ".join(c.get("text", "") for c in item.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "text").strip()


def list_codex_sessions(cwd: str, days: int = 45) -> list:
    """Codex chats that ran in `cwd` (Filect's workspace), newest first, as agent_sessions.SessionInfo."""
    from app.core.agent_sessions import SessionInfo, generate_title
    want = os.path.realpath(cwd)
    out = []
    for f in _session_files(days):
        try:
            first, sid = "", ""
            for i, (t, p) in enumerate(_events(f)):
                if i == 0:
                    if t != "session_meta" or os.path.realpath(p.get("cwd") or "/") != want:
                        break
                    sid = p.get("id") or ""
                it = p.get("item") or {}
                if t == "event_msg" and p.get("type") == "item_completed" and it.get("type") == "UserMessage":
                    first = _user_text(it)
                    break
            if first and sid:
                preview = " ".join(first.split())[:220]
                out.append(SessionInfo(id=sid, cwd=cwd, title=generate_title(first), preview=preview,
                                       mtime=f.stat().st_mtime, project=Path(cwd).name, agent="codex"))
        except Exception:
            continue
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def load_codex_transcript(session_id: str) -> Optional[dict]:
    """A saved Codex chat in load_transcript()'s format: {"steps": [...], "duration": last turn secs}."""
    f = next(iter((codex_home() / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl")), None)
    if f is None:
        return None
    steps, dur = [], None
    try:
        for t, p in _events(f):
            if t != "event_msg":
                continue
            if p.get("type") == "task_complete" and p.get("duration_ms"):
                dur = p["duration_ms"] / 1000.0
            if p.get("type") != "item_completed":
                continue
            it = p.get("item") or {}
            if it.get("type") == "UserMessage":
                if _user_text(it):
                    steps.append(("user", _user_text(it)))
                continue
            for st in _item_steps(it, True, saved=True):
                if st.kind == "text":
                    steps.append(("text", st.text))
                elif st.kind == "tool":
                    steps.append(("tool", st.tool, st.input, st.tool_id))
                else:
                    steps.append(("result", st.tool_id, st.text, st.is_error))
    except Exception:
        return None
    return {"steps": steps, "duration": dur}
