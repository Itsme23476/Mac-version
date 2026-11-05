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
ar.use_login_path()                             # once, for real, before any test stubs subprocess
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
        page._on_codex_info({"models": cr.codex_models(), "update": "", "msg": ""})
        page._focus("x1")
        ov = page._overlay
        names = dict(cr.codex_models())
        assert ov._agent == "codex" and set(ov._model_rows) == set(names), ov._model_rows
        assert list(names.values())[0] in ov._model_btn.text(), ov._model_btn.text()
        assert "Open in Codex" in ov._primary_btn.text()
        page._on_model_changed("gpt-5.6-luna", "medium")
        assert page._models["codex"] == ["gpt-5.6-luna", "medium"] and page._models["claude"][0] == "opus"
        assert settings.assistant_codex_model == "gpt-5.6-luna"
        page._open_task()                                   # this Mac has the Codex desktop app
        assert opened[-1] == ["open", "codex://threads/01a1a990-e8bc-7342-992a-38e47561293a"], opened[-1]
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
    print("Codex chat: Codex's models on the pill (picks saved per agent), 'Open in Codex' → the Codex "
          "desktop app; Claude chat unchanged; rail badges the Codex one  OK")


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


def test_task_runs_by_itself_after_setup():
    """Press "Sign in to Codex" on the card, finish signing in → the waiting task runs on its own (it used to
    say "go back and try again" and wait for you to re-send it). Not if you re-sent it yourself."""
    import subprocess
    page = _page(primary="codex")
    started = []
    page._start_worker = lambda s, rid, seed=True, send=None: started.append((s.key, s.agent, rid, send))
    page._down["claude"] = ("signin", time.time() + 999)        # Claude can't take it either
    real_popen, real_ready = subprocess.Popen, cr.codex_ready
    subprocess.Popen = lambda *a, **k: None                       # don't really open Terminal here
    answers = iter([False, False, True])                          # still signing in… then signed in
    cr.codex_ready = lambda: next(answers, True)
    try:
        sess = ap.AgentSession("w1", tempfile.mkdtemp(), "what is 6 times 7", "Six Times", True, None, "low",
                               agent="codex")
        page._sessions = {"w1": sess}
        page._focused_key = page._last_sent_key = "w1"
        ov = page._ensure_notch()
        page._notch("present")
        page._on_error("w1", ar.CODEX_SETUP_SIGNIN)
        assert "Sign in to Codex" in ov._setup_btn.text() and sess.status == "error"
        ov._setup_btn.click()                                     # the real button
        page._setup_timer.setInterval(30)                         # (2s in the app)
        assert _pump(lambda: started, 5), "the task never ran after sign-in"
        assert started == [("w1", "codex", None, "what is 6 times 7")], started
        assert sess.status == "working" and ov._state == "working" and page._setup_wait is None
        assert "ready" in ov._tool_chip.text(), ov._tool_chip.text()

        # You re-sent it yourself while signing in → it isn't run a second time.
        started.clear()
        s2 = ap.AgentSession("w2", tempfile.mkdtemp(), "again", "Again", True, None, "low", agent="codex")
        page._sessions["w2"] = s2
        page._focused_key = "w2"
        page._on_error("w2", ar.CODEX_SETUP_SIGNIN)
        ov._setup_btn.click()
        s2.status = "working"                                     # (you sent it again)
        page._setup_timer.setInterval(30)
        _pump(lambda: page._setup_wait is None, 5)
        assert started == [], started
    finally:
        subprocess.Popen, cr.codex_ready = real_popen, real_ready
    print("after 'Sign in to Codex', the waiting task runs by itself once you're signed in (not twice)  OK")


def test_stop_while_routing_cancels_it():
    """Stop pressed while your request is still being routed (~3s) used to do nothing — you had to press
    again once the task had started. Now it cancels the request: no task starts."""
    from app.core import intent_router as ir
    page = _page(primary="codex")
    started = []
    page._start_worker = lambda s, rid, seed=True, send=None: started.append(s.key)

    class _SlowRouter:
        def classify(self, text, focused, cands):
            time.sleep(0.6)
            return {"intent": "NEW", "index": None, "task": text}
    real = ir.IntentRouter.get
    ir.IntentRouter.get = classmethod(lambda cls: _SlowRouter())
    try:
        page._notch("rest_idle")
        page.run_voice_task("run sleep 30 then say done")
        assert page._routing and page._overlay._state == "working"
        page._on_stop_task()                                       # pressed during routing
        ov = page._overlay
        assert ov._status_label.text() == "Stopped" and "Stopped by you" in ov._done_view.toPlainText()
        assert not ov._primary_btn.isVisibleTo(ov), "no 'Open in …' for a request that never became a chat"
        _pump(lambda: False, 1.5)                                  # the router answers meanwhile
        assert started == [] and not page._routing, "a stopped request must not start a task"
        page.run_voice_task("what is 2 plus 2")                    # a normal one still runs
        assert _pump(lambda: started, 5), "routing broken after a cancel"
    finally:
        ir.IntentRouter.get = real
    print("Stop while routing cancels the request (no task starts); the next request runs normally  OK")


def test_update_codex_button():
    """An older Codex only knows older models (0.153 had no GPT-6.1-Sol). The model menu offers "Update
    Codex to 0.166.0"; one click runs Codex's own updater and the menu shows the new models."""
    page = _page(primary="codex")
    old = [("gpt-6-astra", "GPT-6-Astra"), ("gpt-5.6-sol", "GPT-5.6-Sol")]
    new = [("gpt-6-astra", "GPT-6-Astra"), ("gpt-6.1-sol", "GPT-6.1-Sol"), ("gpt-6.1-terra", "GPT-6.1 Terra")]
    calls = []
    real = (cr.update_codex, cr.codex_models, cr.codex_update_available)
    cr.update_codex = lambda: (calls.append("update") or (True, "Update ran successfully"))
    cr.codex_models = lambda: new
    cr.codex_update_available = lambda: ""
    try:
        page._on_codex_info({"models": old, "update": "0.166.0", "msg": ""})
        sess = ap.AgentSession("u1", tempfile.mkdtemp(), "hi", "Hi", True, None, "low", agent="codex")
        sess.status = "done"
        page._sessions = {"u1": sess}
        page._focus("u1")
        ov = page._overlay
        assert not ov._update_btn.isHidden() and "0.166.0" in ov._update_btn.text()   # (inside the model menu)
        assert "gpt-6.1-sol" not in ov._model_rows
        ov._update_btn.click()                                     # the real button
        assert "Updating" in ov._update_btn.text() and not ov._update_btn.isEnabled()
        assert _pump(lambda: calls and "gpt-6.1-sol" in ov._model_rows, 10), ov._model_rows
        assert ov._update_btn.isHidden(), "up to date: no update row"
    finally:
        cr.update_codex, cr.codex_models, cr.codex_update_available = real
    print("model menu: 'Update Codex to 0.166.0' → Codex's updater → the newest models appear  OK")


def test_open_in_codex_desktop_app():
    """"Open in Codex" opens the chat in the Codex desktop app (codex://threads/<id>, what Codex's own /app
    does), else Codex in Terminal."""
    import subprocess
    got = []
    real_popen, real_app = subprocess.Popen, cr._desktop_app
    subprocess.Popen = lambda args, **k: got.append(args)
    old = os.environ.pop("CODEX_HOME", None)
    try:
        cr._desktop_app = lambda: True
        cr.open_in_codex("01a1-abc", "/tmp")
        assert got[-1] == ["open", "codex://threads/01a1-abc"], got[-1]
        cr._desktop_app = lambda: False
        cr.open_in_codex("01a1-abc", "/tmp")
        assert got[-1][:3] == ["open", "-a", "Terminal"] and "codex resume 01a1-abc" in Path(got[-1][3]).read_text()
    finally:
        subprocess.Popen, cr._desktop_app = real_popen, real_app
        if old is not None:
            os.environ["CODEX_HOME"] = old
    assert real_app() is True, "this Mac has the Codex desktop app (ChatGPT.app handles codex://)"
    print("Open in Codex → the Codex desktop app (codex://threads/<id>); no app → Terminal  OK")


def test_chat_open_in_codex_app_continues_as_new_chat():
    """Once the Codex app has opened a chat it holds it; a follow-up from Filect used to fail. Now it
    continues as a new Codex chat with a recap."""
    page = _page(primary="codex")
    started = []
    page._start_worker = lambda s, rid, seed=True, send=None: started.append((s.key, s.agent, rid, send))
    s = ap.AgentSession("o1", tempfile.mkdtemp(), "make it darker", "Coffee Site", True, None, "low",
                        resume="01a1-old", agent="codex")
    s.session_id = "01a1-old"
    s.entries = [{"id": "0", "kind": "user", "text": "make a coffee site"},
                 {"id": "1", "kind": "text", "text": "Built index.html."},
                 {"id": "2", "kind": "user", "text": "make it darker"}]
    page._sessions = {"o1": s}
    page._focused_key = "o1"
    page._ensure_notch()
    page._on_error("o1", cr.CHAT_BUSY)
    assert s.status != "error"
    page._on_done("o1")
    k, agent, rid, send = started[-1]
    assert (k, agent, rid) == ("o1", "codex", None) and "Codex: Built index.html." in send and send.endswith("make it darker")
    assert "open in the Codex app" in page._overlay._tool_chip.text(), page._overlay._tool_chip.text()
    print("a chat held by the Codex app continues here as a new Codex chat (with a recap)  OK")


def test_codex_chats_from_codex_index():
    """Codex keeps most chats only in its database (the Codex app's never get files; old files get pruned).
    Filect lists them from there — Codex app + terminal chats for "use my conversation about X", Filect's own
    for the rail — and loads a chat's history from it (for the recap when the Codex app holds that chat)."""
    import json
    import sqlite3
    home = tempfile.mkdtemp()
    ws = tempfile.mkdtemp()
    st = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
    st.execute("create table threads (id text, cwd text, name text, title text, first_user_message text, "
               "preview text, updated_at integer, source text, archived integer)")
    rows = [("app1", "/proj", "", "Email drafts for sponsors", "help me write sponsor emails", "", 300, "vscode", 0),
            ("fil1", ws, "", "make a site", "make a site", "", 200, "exec", 0),
            ("old1", "/x", "", "Archived one", "x", "", 100, "vscode", 1),
            ("rev1", "/x", "", "review", "review this", "", 400, '{"subagent":{"other":"guardian"}}', 0),
            ("tmp1", "/tmp/elsewhere", "", "a test", "a test", "", 500, "exec", 0)]
    st.executemany("insert into threads values (?,?,?,?,?,?,?,?,?)", rows)
    st.commit()
    th = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
    th.execute("create table thread_items (thread_id text, item_json text, rollout_ordinal integer)")
    th.execute("create table thread_turns (thread_id text, duration_ms integer, rollout_ordinal integer)")
    items = [{"type": "userMessage", "content": [{"type": "text", "text": "help me write sponsor emails"}]},
             {"type": "agentMessage", "text": "Sure — short and friendly, signed Damian."},
             {"type": "commandExecution", "id": "c1", "command": "/bin/zsh -lc 'ls'", "aggregatedOutput": "a.txt",
              "exitCode": 0, "status": "completed"},
             {"type": "fileChange", "id": "f1", "changes": [{"path": "/proj/draft.md", "kind": {"type": "add"}}],
              "status": "completed"}]
    th.executemany("insert into thread_items values ('app1', ?, ?)", [(json.dumps(x), i) for i, x in enumerate(items)])
    th.execute("insert into thread_turns values ('app1', 4200, 1)")
    th.commit()
    old = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = home
    try:
        every = cr.codex_threads(ws, everywhere=True)
        assert [t.id for t in every] == ["app1", "fil1"], [t.id for t in every]   # no archived/reviewer/elsewhere
        assert every[0].title == "Email drafts for sponsors" and every[0].agent == "codex"
        assert [t.id for t in cr.codex_threads(ws)] == ["fil1"], "the rail: only Filect's folder"
        tr = cr.load_codex_transcript("app1")
        kinds = [x[0] for x in tr["steps"]]
        assert kinds == ["user", "text", "tool", "result", "tool", "result"], kinds
        assert tr["steps"][3][2] == "a.txt" and tr["steps"][4][1] == "Write" and tr["duration"] == 4.2

        # held by the Codex app, asked for by name in a fresh run → the recap comes from that history
        page = _page(primary="codex")
        sess = ap.AgentSession("n1", "/proj", "draft one for Acme", "Email drafts for sponsors", True, None, "low",
                               resume="app1", agent="codex")
        sess.entries = [{"id": "0", "kind": "user", "text": "draft one for Acme"}]   # nothing else loaded
        recap = page._recap(sess, "codex")
        assert "Me: help me write sponsor emails" in recap and "signed Damian" in recap, recap
    finally:
        if old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old
    print("Codex app + terminal chats come from Codex's index (titles, history); a held chat's recap uses it  OK")


def test_name_match_prefers_the_title():
    """'use my conversation about testing the Filect MCP end to end' must reach the chat TITLED that, not
    one whose long first message just shares common words."""
    from types import SimpleNamespace as NS
    import app.core.agent_sessions as asess
    from app.core import codex_runner as crm
    filler = [NS(id=f"c{i}", title=f"Chat {i}", cwd="/x", project="p", mtime=900 - i, agent="claude",
                 preview="tell me what you think, use the end of the file, what failed and what to test next")
              for i in range(20)]
    target = NS(id="t1", title="Test Filect MCP end to end", cwd="/y", project="q", mtime=1, agent="codex",
                preview="I have the Filect MCP connected. Test it end to end.")
    real = (asess.list_sessions, crm.codex_threads)
    asess.list_sessions = lambda limit=500: list(filler)
    crm.codex_threads = lambda ws, everywhere=False: [target]
    try:
        page = _page()
        page._sessions = {}
        titles = [c["title"] for c in page._build_voice_candidates(
            "Use my conversation about testing the Filect MCP end to end and tell me what failed")]
        assert "Test Filect MCP end to end" in titles, titles
    finally:
        asess.list_sessions, crm.codex_threads = real
    print("resume-by-name: a chat's title outweighs common words in other chats' first messages  OK")


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


def test_real_chat_held_by_codex_app():
    """REAL: a chat the Codex app has open (FILECT_HELD_THREAD) can't be resumed — Filect says so plainly."""
    tid = os.environ.get("FILECT_HELD_THREAD")
    if not tid:
        print("(skipped: set FILECT_HELD_THREAD to a thread the Codex app has open)")
        return
    r = cr.CodexRunner(tempfile.mkdtemp(), on_step=lambda st: None)
    try:
        asyncio.run(r.run("say hi", resume=tid, effort="low"))
        raise AssertionError("resuming a chat held by the Codex app must fail")
    except RuntimeError as e:
        assert str(e) == cr.CHAT_BUSY, str(e)
    print("REAL: Codex's 'already has an active writer' → 'open in the Codex app'  OK")


if __name__ == "__main__":
    test_claude_out_of_usage_is_detected()
    test_primary_and_hand_over()
    test_both_unavailable_shows_the_fixable_one()
    test_codex_chat_on_the_card_and_rail()
    test_refused_claude_attempt_is_not_a_chat()
    test_task_runs_by_itself_after_setup()
    test_stop_while_routing_cancels_it()
    test_update_codex_button()
    test_open_in_codex_desktop_app()
    test_chat_open_in_codex_app_continues_as_new_chat()
    test_codex_chats_from_codex_index()
    test_name_match_prefers_the_title()
    test_real_chat_held_by_codex_app()
    test_real_codex_task_followup_and_stop()
    test_real_signed_out_claude_hands_over_to_codex()
    test_real_codex_routes_a_voice_command()
    print("test_codex_agent OK")
