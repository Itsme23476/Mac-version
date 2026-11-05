"""Codex as the Assistant's second agent: the primary switch, the automatic hand-over when an agent isn't
set up or is out of usage, the Codex setup / Open in Codex buttons, per-agent models, the rail badge —
and REAL Codex runs through the app's own worker thread (task, follow-up, Stop, a real signed-out Claude
handing over to Codex, Codex routing a voice command).
Your settings are never written (save is stubbed); your Claude login is untouched (signed out = an empty
throwaway CLAUDE_CONFIG_DIR). Real Codex tasks run in throwaway folders (~2 min, uses a little of your
ChatGPT plan).
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_codex_agent.py"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication([])
from app.core import agent_runner as ar  # noqa: E402
from app.core import codex_runner as cr  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
settings._save_config = lambda: None            # NEVER write the user's settings.json
ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None
ap.AssistantPage._load_recent = lambda self: None


class _MW:
    pass


def _page(primary="claude"):
    page = ap.AssistantPage(_MW())
    page._primary = primary
    page._models = {"claude": ["opus", "high"], "codex": ["", "low"]}   # low effort: quick real tests
    return page


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.03)
    return cond()


def test_claude_out_of_usage_is_detected():
    """Claude Code's "You've hit your limit" (AssistantMessage.error == rate_limit) → usage, not a Done."""
    import claude_agent_sdk as sdk

    class _Client:
        def __init__(self, options=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def query(self, p): pass

        async def receive_response(self):
            yield sdk.AssistantMessage(content=[sdk.TextBlock("You've hit your limit · resets 3pm (Europe/Athens)")],
                                       model="claude", error="rate_limit")
    real, sdk.ClaudeSDKClient = sdk.ClaudeSDKClient, _Client
    done = []
    try:
        asyncio.run(ar.AgentRunner(tempfile.mkdtemp(), lambda s: None, None,
                                   lambda *a: done.append(a)).run("hi"))
        raise AssertionError("out of usage must raise")
    except ar.AgentSetupError as e:
        assert (e.kind, e.agent) == ("usage", "claude") and "resets 3pm" in str(e), str(e)
        assert ar.setup_info(str(e)) == ("claude", "usage") and done == []
    finally:
        sdk.ClaudeSDKClient = real
    print("Claude 'You've hit your limit · resets 3pm' → usage (with its reset time), no Done card  OK")


def test_primary_and_hand_over():
    page = _page()
    started = []
    page._start_worker = lambda s, rid, seed=True, send=None: started.append((s.key, s.agent, rid, send))
    page.run_task("build a site", cwd=tempfile.mkdtemp())
    assert started[-1][1] == "claude", started
    page._set_primary("codex")
    assert settings.assistant_agent == "codex"
    page.run_task("write a poem", cwd=tempfile.mkdtemp())
    assert started[-1][1] == "codex", "a new chat goes to the primary"
    page._set_primary("claude")

    # Claude isn't installed → the SAME turn runs on Codex, prompt as-is; the chat is now a Codex chat.
    page.run_task("make a logo", cwd=tempfile.mkdtemp())
    key = started[-1][0]
    sess = page._sessions[key]
    page._on_error(key, ar.SETUP_INSTALL)
    assert sess.status != "error", "no error card while the other agent can take over"
    page._on_done(key)
    assert started[-1] == (key, "codex", None, "make a logo"), started[-1]     # as-is: no recap
    assert sess.agent == "codex" and sess.fell_back and "claude" in page._down
    page.run_task("another task", cwd=tempfile.mkdtemp())
    assert started[-1][1] == "codex", "while Claude is known unusable, new chats go straight to Codex"

    # A CONTINUED Claude chat runs out of usage → Codex starts a new chat WITH a recap of this one.
    page._down.clear()
    s2 = ap.AgentSession("c2", tempfile.mkdtemp(), "add a footer", "Coffee Site", True, "opus", "high",
                         resume="sid-old", agent="claude")
    s2.entries = [{"id": "0", "kind": "user", "text": "make a coffee site"},
                  {"id": "1", "kind": "text", "text": "Built index.html with a hero."},
                  {"id": "2", "kind": "user", "text": "add a footer"}]
    page._sessions["c2"] = s2
    page._on_error("c2", ar.usage_message("claude", "You've hit your limit · resets 3pm"))
    page._on_done("c2")
    k, agent, rid, send = started[-1]
    assert (k, agent, rid) == ("c2", "codex", None), started[-1]
    assert "Me: make a coffee site" in send and "Claude Code: Built index.html" in send, send
    assert send.endswith("add a footer") and s2.resume is None and s2.session_id == ""
    assert page._down["claude"][0] == "usage"

    # One hand-over per turn (no ping-pong); Stop never hands over.
    n = len(started)
    page._on_error("c2", ar.usage_message("codex", "Try again at 5 PM."))
    page._on_done("c2")
    assert len(started) == n and s2.status == "error"
    assert (s2.agent, s2.resume) == ("claude", "sid-old"), "no hand-over after all: back to its own agent + chat"
    s3 = ap.AgentSession("c3", tempfile.mkdtemp(), "x", "X", True, "opus", "high")
    page._sessions["c3"] = s3
    page._down.clear()
    s3.stopping = True
    page._on_error("c3", ar.SETUP_SIGNIN)
    assert s3.fallback_to is None, "a stopped task doesn't hand over"
    print("primary → new chats; not set up / out of usage → the same turn hands over (recap for a continued "
          "chat), once, never on Stop; known-down agent skipped next time  OK")


def test_both_unavailable_shows_the_fixable_one():
    import subprocess
    page = _page()
    page._start_worker = lambda *a, **k: None                      # the hand-over itself isn't run here
    opened = []
    real = subprocess.Popen
    subprocess.Popen = lambda args, **k: opened.append(args)       # capture: don't open Terminal here
    try:
        sess = ap.AgentSession("b1", tempfile.mkdtemp(), "do it", "Do It", True, "opus", "high")
        page._sessions = {"b1": sess}
        page._focused_key = page._last_sent_key = "b1"
        ov = page._ensure_notch()
        page._notch("present")
        page._on_error("b1", ar.usage_message("claude", "You've hit your limit · resets 3pm"))
        page._on_done("b1")                       # → Codex (stubbed below: it isn't installed)
        assert sess.agent == "codex"
        page._on_error("b1", ar.CODEX_SETUP_INSTALL)
        assert ov._status_label.text() == "Setup needed" and "Set up Codex" in ov._setup_btn.text()
        assert sess.agent == "claude" and ov._agent == "claude", "the chat stays with its own agent"
        assert ov._error_label.text() == ar.CODEX_SETUP_INSTALL
        page._down["codex"] = ("install", time.time() + 999)
        ov._setup_btn.click()                     # the real button → page → Terminal
        assert "codex" not in page._down, "clicking set-up retries Codex next time"
        script = Path(opened[-1][3]).read_text()
        assert opened[-1][:3] == ["open", "-a", "Terminal"]
        assert "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh" in script
        assert "codex login" in script and "All set" in script
        page._on_error("b1", ar.CODEX_SETUP_SIGNIN)
        assert "Sign in to Codex" in ov._setup_btn.text()
        ov._setup_btn.click()
        assert "install.sh" not in Path(opened[-1][3]).read_text()
        b2 = ap.AgentSession("b2", tempfile.mkdtemp(), "again", "Again", True, "opus", "high")
        page._sessions["b2"] = b2
        page._focused_key = page._last_sent_key = "b2"
        page._down.clear()
        page._on_error("b2", ar.usage_message("claude", "You've hit your limit · resets 3pm"))
        page._on_done("b2")                        # → Codex, which is out of usage too
        page._on_error("b2", ar.usage_message("codex", "Try again at 5 PM."))   # BOTH out of usage
        assert ov._status_label.text() == "Out of usage" and not ov._setup_btn.isVisibleTo(ov)
        assert not ov._primary_btn.isVisibleTo(ov)
        assert "resets 3pm" in ov._error_label.text() and "5 PM" in ov._error_label.text(), \
            "both agents' reset times"
    finally:
        subprocess.Popen = real
    print("both can't run → the card shows the one you can fix ('Set up / Sign in to Codex' → Terminal with "
          "OpenAI's installer + codex login); out of usage says when, no button  OK")


def test_codex_chat_on_the_card_and_rail():
    import subprocess
    page = _page()
    opened = []
    real = subprocess.Popen
    subprocess.Popen = lambda args, **k: opened.append(args)
    try:
        cx = ap.AgentSession("x1", "/tmp/proj dir", "build", "Build", True, None, "low", agent="codex")
        cl = ap.AgentSession("x2", "/tmp", "plan", "Plan", True, "opus", "high")
        cx.status = cl.status = "done"
        cx.session_id = "01a1a990-e8bc-7342-992a-38e47561293a"
        page._sessions = {"x1": cx, "x2": cl}
        page._focus("x1")
        ov = page._overlay
        names = dict(cr.codex_models())
        assert ov._agent == "codex" and set(ov._model_rows) == set(names), ov._model_rows
        assert list(names.values())[0] in ov._model_btn.text(), ov._model_btn.text()
        assert "Open in Codex" in ov._primary_btn.text()
        page._on_model_changed("gpt-5.6-luna", "medium")
        assert page._models["codex"] == ["gpt-5.6-luna", "medium"] and page._models["claude"][0] == "opus"
        assert settings.assistant_codex_model == "gpt-5.6-luna"
        page._open_task()
        script = Path(opened[-1][3]).read_text()
        assert "codex resume 01a1a990-e8bc-7342-992a-38e47561293a" in script and "cd '/tmp/proj dir'" in script
        page._focus("x2")
        assert ov._agent == "claude" and "Opus" in ov._model_btn.text() and "Open in Claude" in ov._primary_btn.text()
        rail = page._ensure_rail()
        page._refresh_rail()
        rows = {k[1]: r for k, r in rail._row_cache.items()}
        assert rows["x1"]._tag.isVisibleTo(rows["x1"]) and not rows["x2"]._tag.isVisibleTo(rows["x2"])
        out = os.environ.get("FILECT_TEST_SHOTS")
        if out:
            rail.grab().save(os.path.join(out, "rail_codex_badge.png"))
    finally:
        subprocess.Popen = real
    print("Codex chat: Codex's models on the pill (picks saved per agent), 'Open in Codex' → Terminal "
          "`codex resume <id>` in its folder; Claude chat unchanged; rail badges the Codex one  OK")


def test_refused_claude_attempt_is_not_a_chat():
    """A task Claude refused (signed out / out of usage) and handed to Codex left a Claude chat file whose
    only reply is an API error — it showed in the rail as a dead twin of the Codex chat. Not any more;
    a real chat that later hit the limit still shows."""
    import json
    from app.core import agent_sessions as asess
    home = tempfile.mkdtemp()
    proj = Path(home) / "projects" / "-tmp-ws"
    proj.mkdir(parents=True)

    def chat(name, *assistant):
        rows = [{"type": "user", "cwd": "/tmp/ws", "message": {"role": "user", "content": f"task {name}"}}]
        rows += [dict({"type": "assistant", "cwd": "/tmp/ws",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}, **extra)
                 for t, extra in assistant]
        (proj / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    err = {"isApiErrorMessage": True, "error": "rate_limit"}
    chat("refused", ("You've hit your limit · resets 3pm", err))
    chat("real", ("Done — built it.", {}))
    chat("real-then-limit", ("Done — built it.", {}), ("You've hit your limit · resets 3pm", err))
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = home
    try:
        ids = sorted(s.id for s in asess.list_sessions(cwd="/tmp/ws"))
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old
    assert ids == ["real", "real-then-limit"], ids
    print("a refused Claude attempt (only an API error) isn't listed as a chat; real chats are  OK")


def test_real_codex_task_followup_and_stop():
    page = _page(primary="codex")
    tmp = tempfile.mkdtemp(prefix="filect-codex-")
    page.run_task("Create hello.md containing just the word ok. Then say done.", cwd=tmp)
    key = page._last_sent_key
    sess = page._sessions[key]
    assert sess.agent == "codex"
    assert _pump(lambda: sess.status in ("done", "error") and not sess.running, 180), sess.status
    assert sess.status == "done", sess.result_text
    assert (Path(tmp) / "hello.md").read_text().strip() == "ok"
    tools = [e.get("tool") for e in sess.entries if e.get("kind") == "tool"]
    assert len(sess.session_id) == 36, sess.session_id
    assert tools, f"the action that wrote hello.md shows in Details: {sess.entries}"
    assert page._overlay._state == "done" and "Open in Codex" in page._overlay._primary_btn.text()
    sid = sess.session_id
    page.run_task("What is the file called? Filename only.", resume=sid, cwd=tmp)       # follow-up
    assert page._last_sent_key == key, "a follow-up reuses the same chat"
    assert _pump(lambda: sess.status == "done" and not sess.running, 120), sess.status
    assert sess.session_id == sid and "hello.md" in sess.result_text, (sess.session_id, sess.result_text)

    page.run_task("Run the shell command `sleep 30`, then say finished.", cwd=tempfile.mkdtemp())
    s2 = page._sessions[page._last_sent_key]
    assert _pump(lambda: any(e.get("kind") == "tool" for e in s2.entries), 120), "never started the command"
    t = time.time()
    page._on_stop_task()
    assert _pump(lambda: not s2.running, 15) and s2.status == "stopped", s2.status
    ov = page._overlay
    assert ov._status_label.text() == "Stopped" and "Stopped by you" in ov._done_view.toPlainText(), \
        "a stopped task says so — not the agent's last line + 'Finished'"
    assert "Finished" not in ov._done_view.toPlainText()
    print(f"REAL Codex via the app's worker: task done (file written, its action in Details, Open in Codex), "
          f"follow-up continued the same chat, Stop ended it in {time.time() - t:.1f}s  OK")
    page.shutdown()


def test_real_signed_out_claude_hands_over_to_codex():
    page = _page(primary="claude")
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()      # a real, signed-out Claude Code
    try:
        page.run_task("Reply with exactly: handed over ok", cwd=tempfile.mkdtemp())
        sess = page._sessions[page._last_sent_key]
        assert _pump(lambda: sess.status in ("done", "error") and not sess.running and sess.agent == "codex"
                     and not sess.fallback_to, 180), (sess.status, sess.agent)
        assert sess.status == "done" and "handed over ok" in sess.result_text.lower(), sess.result_text
        assert page._down["claude"][0] == "signin"
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old
    print("REAL: Claude Code signed out → the same task ran on Codex and finished  OK")
    page.shutdown()


def test_real_codex_routes_a_voice_command():
    from app.core.intent_router import classify_with_codex
    t = time.time()
    d = classify_with_codex("start a new task: write a haiku about rain", "Coffee Site", ["Coffee Site"])
    assert d["intent"] == "NEW" and "haiku" in d["task"].lower(), d
    d2 = classify_with_codex("make the footer darker", "Coffee Site", ["Coffee Site"])
    assert d2["intent"] == "FOLLOW_UP", d2
    print(f"REAL: Codex routes voice when Claude can't (NEW / FOLLOW_UP right, {(time.time() - t) / 2:.1f}s "
          "each)  OK")


if __name__ == "__main__":
    test_claude_out_of_usage_is_detected()
    test_primary_and_hand_over()
    test_both_unavailable_shows_the_fixable_one()
    test_codex_chat_on_the_card_and_rail()
    test_refused_claude_attempt_is_not_a_chat()
    test_real_codex_task_followup_and_stop()
    test_real_signed_out_claude_hands_over_to_codex()
    test_real_codex_routes_a_voice_command()
    print("test_codex_agent OK")
