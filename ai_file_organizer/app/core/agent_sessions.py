"""Browse local Claude Code sessions so the Assistant can list and RESUME existing conversations —
across ALL projects, not just its own workspace.

These are the same JSONL files Claude Code and Claude Desktop keep under ~/.claude/projects/
(<encoded-cwd>/<session-id>.jsonl). Read-only: we never write here. Resuming one is just
AgentRunner(cwd=<session.cwd>).run(prompt, resume=<session.id>) — Claude continues that chat with
its full prior context, and the new prompt is the next turn.

This is the engine the task-management UI (and "use my <name> conversation and do X" by voice) sits
on; the UI is a later step.

Headless self-check:
    cd /Users/damianosmalliaros/Developer/Mac_app && \
      venv311/bin/python ai_file_organizer/app/core/agent_sessions.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


@dataclass
class SessionInfo:
    id: str          # session id (jsonl filename stem) -> pass as resume=
    cwd: str         # the folder the conversation ran in -> pass as cwd=
    title: str       # Claude Code's generated summary if present, else the first user line
    preview: str     # first user message, trimmed
    mtime: float     # last-modified epoch (recency / sort key)
    project: str     # basename of cwd, for grouping/display
    agent: str = "claude"   # "claude" (~/.claude/projects) or "codex" (~/.codex/sessions, codex_runner)

    @property
    def when(self) -> str:
        return time.strftime("%b %d, %H:%M", time.localtime(self.mtime))


def _extract_text(message) -> str:
    """Plain text from a user message whose content is a str OR a list of blocks."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(p for p in parts if p)
    return ""


def _decode_cwd(dirname: str) -> str:
    # Fallback only: the folder name encodes the cwd with separators collapsed to '-', which is
    # lossy (can't tell a space from a slash). We prefer the explicit `cwd` field inside the file.
    return "/" + dirname.strip("-").replace("-", "/")


def _custom_title(jsonl_path: Path) -> Optional[str]:
    """Claude Code keeps the sidebar title the user sees in a sibling file:
    <proj>/<session-id>/custom-title.json -> {"customTitle": "..."}. (The jsonl itself has no
    summary entry, so this is the only real title source.)"""
    tf = jsonl_path.parent / jsonl_path.stem / "custom-title.json"
    try:
        if tf.is_file():
            t = (json.loads(tf.read_text(encoding="utf-8")).get("customTitle") or "").strip()
            return t or None
    except Exception:
        return None
    return None


def _parse_session(path: Path) -> Optional[SessionInfo]:
    summary: Optional[str] = None
    first_user: Optional[str] = None
    cwd: Optional[str] = None
    had_content = False
    replied = api_error = False          # a real reply / only an API error ("Not logged in", out of usage)
    read_all = True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "summary" and not summary:
                    summary = (obj.get("summary") or "").strip()
                if cwd is None and obj.get("cwd"):
                    cwd = obj.get("cwd")
                if t in ("user", "assistant"):
                    had_content = True
                    if t == "user" and first_user is None:
                        first_user = _extract_text(obj.get("message"))
                    if t == "assistant":
                        api_error |= bool(obj.get("isApiErrorMessage"))
                        replied |= not obj.get("isApiErrorMessage")
                # Bounded read: a listing row needs a title source + cwd. The summary (when it
                # exists) sits near the top, so once we have a first user line + cwd and have
                # scanned a bit, stop — don't read megabytes of a long transcript per row.
                if (first_user and cwd) and (summary or lineno > 60):
                    read_all = False
                    break
    except Exception:
        return None
    if not had_content and not summary:
        return None
    if read_all and api_error and not replied:
        # Claude Code refused to run (signed out / out of usage) — the task went to the other agent;
        # this attempt isn't a conversation (it showed as a dead twin of the Codex chat).
        return None
    preview = (first_user or "").strip().replace("\n", " ")
    ct = _custom_title(path)
    title = (ct or summary or (preview[:60] if preview else path.stem[:8])).strip()
    if not cwd:
        cwd = _decode_cwd(path.parent.name)
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return SessionInfo(id=path.stem, cwd=cwd, title=title[:90],
                       preview=preview[:220], mtime=mtime, project=(Path(cwd).name or cwd))


def list_sessions(limit: int = 100, cwd: Optional[str] = None) -> List[SessionInfo]:
    """All local Claude Code sessions, newest first. Pass `cwd` to scope to one project folder."""
    root = _projects_root()
    if not root.exists():
        return []
    want = os.path.normpath(cwd) if cwd else None
    out: List[SessionInfo] = []
    try:
        projects = list(root.iterdir())
    except Exception:
        return []
    if want:
        # Fast path: Claude Code names a cwd's folder by swapping every non-alphanumeric for '-', so
        # read just that folder instead of parsing every session on disk (hundreds). The cwd check
        # below still applies, since the encoding is lossy; full scan if the folder isn't there.
        enc = root / re.sub(r"[^A-Za-z0-9]", "-", want)
        if enc.is_dir():
            projects = [enc]
    for proj in projects:
        if not proj.is_dir():
            continue
        try:
            files = list(proj.glob("*.jsonl"))
        except Exception:
            continue
        for f in files:
            info = _parse_session(f)
            if info is None:
                continue
            if want and os.path.normpath(info.cwd) != want:
                continue
            out.append(info)
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out[:limit]


# ---- chats the Assistant continued OUTSIDE its workspace ------------------------------------------
# New tasks run in the workspace folder, which the rail lists. A chat CONTINUED elsewhere (a resumed
# project chat) lives in that project's folder, so after a restart nothing pointed to it and it
# dropped off the rail. Remember those ids here (app data dir, never ~/.claude).
_TOUCHED_MAX = 200


def _touched_path() -> Path:
    from app.core.settings import settings
    return settings.get_app_data_dir() / "assistant_sessions.json"


def _read_touched() -> Optional[list]:
    """The remembered [{id, cwd}], or None if the file was never written (→ run the backfill)."""
    try:
        data = json.loads(_touched_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return None
    except Exception:
        return []


def _write_touched(items: list) -> None:
    try:
        _touched_path().write_text(json.dumps(items[:_TOUCHED_MAX]), encoding="utf-8")
    except Exception:
        pass


def remember_session(session_id: str, cwd: str) -> None:
    """Keep a chat continued outside the workspace on the rail after restarts. Never raises."""
    if not session_id:
        return
    items = [e for e in (_read_touched() or []) if isinstance(e, dict) and e.get("id") != session_id]
    _write_touched([{"id": session_id, "cwd": cwd}] + items)


def _backfill(skip_dir: str) -> list:
    """One-time catch-up for chats continued before remember_session existed: outside the workspace,
    written to by the Python SDK (Filect) AND by Claude Code/Desktop — so a real continued chat, not
    an intent-router or test leftover. Reads every transcript once (~1s per GB), off the UI thread."""
    out = []
    sdk_re = re.compile(rb'"entrypoint":\s*"sdk-py"')
    other_re = re.compile(rb'"entrypoint":\s*"(?:cli|claude-desktop)"')
    for f in _projects_root().glob("*/*.jsonl"):
        if f.parent.name == skip_dir:
            continue
        sdk = other = False
        try:
            with f.open("rb") as fh:
                for line in fh:
                    if b'"entrypoint"' not in line:     # cheap pre-check; most lines skip the regex
                        continue
                    if sdk_re.search(line):
                        sdk = True
                    elif other_re.search(line):
                        other = True
                    if sdk and other:
                        info = _parse_session(f)
                        if info:
                            out.append({"id": info.id, "cwd": info.cwd})
                        break
        except Exception:
            continue
    return out


def remembered_sessions(workspace: str) -> List[SessionInfo]:
    """The chats continued outside `workspace` that still exist on disk, newest first."""
    items = _read_touched()
    if items is None:                    # first run with this feature: find the ones from before it
        items = _backfill(re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(workspace)))
        _write_touched(items)
    out = []
    for e in items:
        sid = e.get("id") if isinstance(e, dict) else None
        f = _session_file(sid, e.get("cwd")) if sid else None
        info = _parse_session(f) if f else None
        if info:
            out.append(info)
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def tool_result_text(content) -> str:
    """Plain text of a tool result's content (a str, or a list of text/image blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[image]")
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(p for p in parts if p)


def _parse_ts(s) -> Optional[float]:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _session_file(session_id: str, cwd: Optional[str]) -> Optional[Path]:
    root = _projects_root()
    if cwd:                                      # same folder encoding as list_sessions' fast path
        p = root / re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd)) / f"{session_id}.jsonl"
        if p.exists():
            return p
    return next(iter(root.glob(f"*/{session_id}.jsonl")), None)


def load_transcript(session_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    """What happened in a saved conversation, for showing it in the notch: `steps` in order —
    ("user", text) what you said, ("text", text) Claude's replies, ("tool", name, input, id) its
    actions, ("result", tool_id, text, is_error) what each action returned — plus `duration`, the LAST
    turn's length in seconds (None if unknown). Thinking and injected stubs are skipped. Reads the
    whole file, so call it off the UI thread."""
    path = _session_file(session_id, cwd)
    if path is None:
        return None
    steps, turn_start, last_ts = [], None, None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t not in ("user", "assistant") or obj.get("isSidechain"):   # sidechain = a subagent's own chat
                    continue
                ts = _parse_ts(obj.get("timestamp"))
                meta = bool(obj.get("isMeta"))       # injected notes ("[Image: original 390x3000…]")
                content = (obj.get("message") or {}).get("content")
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    text = (b.get("text") or "").strip() if bt == "text" else ""
                    if t == "user" and text and not meta and not text.startswith("<") \
                            and not text.startswith("This session is being continued"):
                        steps.append(("user", text))
                        turn_start = ts                  # duration = the last turn
                    elif t == "assistant" and text:
                        steps.append(("text", text))
                    elif t == "assistant" and bt == "tool_use":
                        steps.append(("tool", b.get("name") or "", b.get("input") or {}, b.get("id") or ""))
                    elif t == "user" and bt == "tool_result":
                        steps.append(("result", b.get("tool_use_id") or "",
                                      tool_result_text(b.get("content")), bool(b.get("is_error"))))
                if ts:
                    last_ts = ts
    except Exception:
        return None
    dur = last_ts - turn_start if (turn_start and last_ts and last_ts >= turn_start) else None
    return {"steps": steps, "duration": dur}


def find_by_name(query: str, limit: int = 5) -> List[SessionInfo]:
    """Fuzzy-ish lookup for 'use my <name> conversation' — case-insensitive substring over the
    title/preview, newest first. The voice-by-name path will call this."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = [s for s in list_sessions(limit=500)
            if q in s.title.lower() or q in s.preview.lower()]
    return hits[:limit]


def _strip_code_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


# Spoken cues that signal "continue an existing conversation". With NONE of these present it's a
# plain task -> we skip the LLM entirely and start NEW, so an unnamed command can never resume (or
# interfere with) a past conversation. Keep broad but reference-ish.
_RESUME_CUES = (
    "conversation", "chat", "thread", "continue", "resume", "go back", "pick up", "pick back up",
    "use my", "open my", "that one", "the one we", "earlier", "previous", "last time",
    "where we left", "we were working", "we discussed", "we talked",
)


def route_speech(text: str, max_candidates: int = 40) -> dict:
    """Decide whether a spoken command means CONTINUE an existing conversation or start a NEW one,
    and split out the task. Returns {"session": SessionInfo|None, "task": str}.

    Conservative by design: ONLY resumes when the user explicitly refers to a prior conversation.
    No reference cue -> NEW, with no LLM call at all. The LLM call (only when a cue is present) goes
    through the same Supabase openai-proxy the app's other AI uses (key server-side) and is told to
    default to NEW when unsure. ANY failure falls back to NEW with the full text, so speech always
    does something and never silently resumes the wrong chat.

    ponytail: cue gate first (free, zero false resumes), one cheap nano call only when it might be
    a resume.
    """
    text = (text or "").strip()
    if not text:
        return {"session": None, "task": ""}
    low = text.lower()
    if not any(cue in low for cue in _RESUME_CUES):
        logger.info("[assistant-route] no resume cue -> NEW conversation")
        return {"session": None, "task": text}
    sessions = list_sessions(limit=max_candidates)
    if not sessions:
        return {"session": None, "task": text}
    listing = "\n".join(f"{i}. {s.title} (in {s.project})" for i, s in enumerate(sessions))
    system = (
        "You route a spoken command for a local coding assistant. DEFAULT to starting a NEW task. "
        "Only choose an existing conversation when the user EXPLICITLY asks to continue/use/open a "
        "specific earlier conversation (e.g. 'use my X conversation', 'continue my X', 'go back to "
        "the X chat'). If they are merely describing a task — even one whose topic resembles an "
        "existing conversation — return index null (NEW). When unsure, return null. "
        'Respond with ONLY compact JSON: {"index": <conversation number, or null>, '
        '"task": "<the instruction to carry out, with any \'use my X conversation\' phrasing removed>"}.'
    )
    user = f"Command: {text}\n\nConversations:\n{listing}"
    try:
        from app.core.vision import _call_openai_proxy
        resp = _call_openai_proxy(
            "chat",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=220, temperature=0.0)
        content = resp["choices"][0]["message"]["content"]
        data = json.loads(_strip_code_fence(content))
        idx = data.get("index")
        task = (data.get("task") or text).strip() or text
        if isinstance(idx, int) and 0 <= idx < len(sessions):
            s = sessions[idx]
            logger.info(f"[assistant-route] RESUME '{s.title}' (idx={idx}, cwd={s.cwd}) task={task!r}")
            return {"session": s, "task": task}
        logger.info(f"[assistant-route] LLM chose NEW (index={idx!r}) task={task!r}")
        return {"session": None, "task": task}
    except Exception as e:
        logger.warning(f"[assistant-route] routing failed ({e}) -> NEW")
        return {"session": None, "task": text}


# Politeness / glue words to drop so a spoken prompt collapses to its content words. Kept small and
# generic — it just has to make "hi go build me a small one page website" read as "Build Small One
# Page Website". ponytail: a word set + Title Case beats a cloud call for a cosmetic label.
_TITLE_FILLERS = {
    "a", "an", "the", "to", "for", "of", "and", "or", "my", "me", "i", "you", "your", "we", "us",
    "it", "that", "this", "there", "hi", "hey", "hello", "ok", "okay", "so", "um", "uh", "please",
    "pls", "can", "could", "would", "should", "now", "just", "go", "let", "lets", "want", "wanna",
    "gonna", "need", "like", "then", "actually", "basically", "really", "kinda", "maybe", "some",
    "be", "is", "are", "am", "do", "does", "get", "got", "with", "on", "in", "into", "up", "all",
    "them", "their", "its",
}


def generate_title(prompt: str, max_words: int = 5) -> str:
    """A short title for a task, built LOCALLY from its prompt — no AI, no network, instant. Takes the
    first sentence, drops filler/politeness words, keeps the content words and Title-Cases them
    (preserving acronyms like TODO/API/CSV). Always non-empty for a non-empty prompt.

    "hi go build me a small one page website" -> "Build Small One Page Website".
    """
    import re
    text = re.sub(r"\[screenshot \d+\]", " ", prompt or "").strip()   # receive-mode markers
    if not text:
        return ""
    head = re.split(r"[.!?\n]", text, 1)[0]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'+#.-]*", head)
    content = [w for w in words if w.lower() not in _TITLE_FILLERS]
    picked = (content or words)[:max_words]
    if not picked:
        return text[:46].rstrip()
    title = " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in picked)
    return title[:48].rstrip()


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    # 1) Parser correctness on a synthetic session (summary + user text + cwd + list-content).
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "-Users-me-Demo"
        proj.mkdir()
        sess = proj / "abc123.jsonl"
        sess.write_text(
            json.dumps({"type": "summary", "summary": "Sponsor inquiry email drafts"}) + "\n"
            + json.dumps({"type": "user", "cwd": "/Users/me/Demo",
                          "message": {"role": "user",
                                      "content": [{"type": "text", "text": "Draft a reply to the brand"}]}}) + "\n"
            + json.dumps({"type": "assistant", "cwd": "/Users/me/Demo",
                          "message": {"role": "assistant", "content": "Sure"}}) + "\n",
            encoding="utf-8")
        info = _parse_session(sess)
        assert info is not None
        assert info.id == "abc123", info.id
        assert info.title == "Sponsor inquiry email drafts", info.title
        assert info.cwd == "/Users/me/Demo", info.cwd
        assert "Draft a reply" in info.preview, info.preview
        assert info.project == "Demo", info.project

        # A user message with plain-string content, no summary -> title falls back to first line.
        s2 = proj / "def456.jsonl"
        s2.write_text(json.dumps({"type": "user", "cwd": "/Users/me/Demo",
                                  "message": {"role": "user", "content": "Fix the login bug"}}) + "\n",
                      encoding="utf-8")
        i2 = _parse_session(s2)
        assert i2 is not None and i2.title == "Fix the login bug", i2.title

        # An empty / junk file -> skipped (None).
        s3 = proj / "empty.jsonl"
        s3.write_text("\n\nnot json\n", encoding="utf-8")
        assert _parse_session(s3) is None

    print("agent_sessions parser self-check OK")

    # 2) route_speech must NEVER raise and must fall back to a NEW conversation when the LLM
    #    isn't reachable (as here, standalone — no app/auth), so speech always does something.
    assert _strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'
    r = route_speech("use my sponsorship conversation and draft a follow-up")
    assert r["session"] is None and "sponsorship" in r["task"].lower(), r
    print("route_speech fallback + fence-strip OK")

    # generate_title: local, no AI — drops filler, keeps content words, preserves acronyms.
    assert generate_title("") == ""
    assert generate_title("hi go build me a small one page website for filect") \
        == "Build Small One Page Website", generate_title("hi go build me a small one page website for filect")
    assert generate_title("find all the TODO comments in my project and list them") \
        == "Find TODO Comments Project List", generate_title("find all the TODO comments in my project and list them")
    assert len(generate_title("x" * 200)) <= 48
    print("generate_title (local) OK")

    # 3) Informational: list the user's REAL local sessions (read-only).
    real = list_sessions(limit=8)
    print(f"\nFound {len(list_sessions(limit=10000))} local Claude Code sessions. Most recent:")
    for s in real:
        print(f"  [{s.when}]  {s.project:<22.22}  {s.title}")
