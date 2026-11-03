"""Rail history + "select a conversation → see it in the notch" (VoiceOS behaviour).
(1) list_sessions(cwd=...) reads ONLY the workspace folder (fast path);
(2) load_transcript turns a saved .jsonl into what-you-said / Claude's replies / actions + the last
    turn's duration (tool results, thinking, injected stubs skipped);
(3) _group: RUNNING pinned, then Today / Previous 7 days / Previous 30 days (calendar), then per month;
(4) the rail renders those groups and a row click emits the right signal;
(5) the real point — clicking a PAST chat's row loads its transcript on a real thread and drops the
    notch down showing it (what you said, Claude's reply, the actions, real duration — not 0:00),
    without bumping it to Today; then speaking continues THAT claude session. A running chat's row
    opens the notch on its live view.
Cost-free (temp files, stubbed worker). Run:
    QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_rail_recent.py
"""
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication.instance() or QApplication([])

import app.core.agent_sessions as asess
from app.ui import assistant_page as ap
from app.ui.conversation_rail import ConversationRail, _group
ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None   # don't spawn notch/rail on a timer
_REAL_LOAD_RECENT = ap.AssistantPage._load_recent
ap.AssistantPage._load_recent = lambda self: None          # tests feed _recent directly
asess._touched_path = lambda: Path(tempfile.mkdtemp()) / "assistant_sessions.json"   # never the real app dir

WS = "/tmp/ws/Filect Assistant"
WS_DIR = "-tmp-ws-Filect-Assistant"


def pump(ms):
    t = time.time()
    while time.time() - t < ms / 1000:
        app.processEvents(); time.sleep(0.01)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _write_lines(path: Path, entries, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))


def _user(text, ts):
    return {"type": "user", "cwd": WS, "timestamp": _iso(ts), "message": {"role": "user", "content": text}}


def _asst(block, ts):
    return {"type": "assistant", "timestamp": _iso(ts), "message": {"role": "assistant", "content": [block]}}


def _fern_transcript(t0):
    """A realistic two-turn chat: thinking, tool calls + results, replies, an injected stub."""
    return [_user("Build me a site", t0),
            _asst({"type": "thinking", "thinking": "plan"}, t0 + 1),
            _asst({"type": "tool_use", "name": "Write", "input": {"file_path": "/x/index.html"}}, t0 + 5),
            {"type": "user", "timestamp": _iso(t0 + 6),
             "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            _asst({"type": "text", "text": "Done — built it."}, t0 + 10),
            _user("<command-name>/model</command-name>", t0 + 50),
            _user("make it blue", t0 + 100),
            _asst({"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/style.css"}}, t0 + 105),
            _asst({"type": "text", "text": "Made it blue."}, t0 + 130)]


def test_fast_path():
    root = Path(tempfile.mkdtemp())
    now = time.time()
    _write_lines(root / WS_DIR / "old.jsonl", [_user("first chat", now)], now - 500)
    _write_lines(root / WS_DIR / "new.jsonl", [_user("second chat", now)], now - 100)
    _write_lines(root / "-tmp-other" / "x.jsonl", [dict(_user("unrelated", now), cwd="/tmp/other")], now)
    asess._projects_root = lambda: root
    parsed, real_parse = [], asess._parse_session
    asess._parse_session = lambda p: (parsed.append(p.name), real_parse(p))[1]
    got = asess.list_sessions(limit=30, cwd=WS)
    assert [s.id for s in got] == ["new", "old"], [s.id for s in got]
    assert len(parsed) == 2, f"fast path should parse only the workspace folder, parsed {parsed}"
    parsed.clear()
    assert asess.list_sessions(limit=30, cwd="/nope/missing") == [] and len(parsed) == 3
    asess._parse_session = real_parse
    print("fast path: reads only the workspace folder; full scan fallback  OK")


def test_load_transcript():
    root = Path(tempfile.mkdtemp())
    asess._projects_root = lambda: root
    t0 = time.time() - 3600
    _write_lines(root / WS_DIR / "sid-fern.jsonl", _fern_transcript(t0))
    d = asess.load_transcript("sid-fern", WS)
    kinds = [s[0] for s in d["steps"]]
    assert kinds == ["user", "tool", "result", "text", "user", "tool", "text"], kinds
    assert d["steps"][0] == ("user", "Build me a site") and d["steps"][4] == ("user", "make it blue")
    assert d["steps"][2][0] == "result" and d["steps"][2][2] == "ok"           # the write's result
    assert abs(d["duration"] - 30) < 1, d["duration"]               # the LAST turn: 100s → 130s
    assert asess.load_transcript("sid-fern", None) is not None        # found without cwd (glob)
    assert asess.load_transcript("missing", WS) is None
    print("load_transcript: replies + actions, skips results/thinking/stubs, last-turn duration  OK")


def test_group():
    now = datetime(2026, 11, 2, 15, 0).timestamp()                   # 3pm local
    running = NS(key="r", status="working", started_at=now - 60, title="Running one")
    done = NS(key="d", status="done", started_at=now - 3600, title="Finished this run")
    recent = [NS(id="t", mtime=now - 7200, title="Today past"),
              NS(id="w", mtime=now - 3 * 86400, title="This week"),
              NS(id="m", mtime=now - 20 * 86400, title="This month"),
              NS(id="s", mtime=now - 40 * 86400, title="September chat"),   # Sep 23 2026
              NS(id="a", mtime=now - 70 * 86400, title="August chat"),      # Aug 24 2026
              NS(id="y", mtime=now - 400 * 86400, title="Last year")]       # Sep 28 2025
    g = _group([running, done], recent, now)
    assert [(h, [o.title for _, o in items]) for h, items in g] == [
        ("RUNNING", ["Running one"]),
        ("TODAY", ["Finished this run", "Today past"]),
        ("PREVIOUS 7 DAYS", ["This week"]),
        ("PREVIOUS 30 DAYS", ["This month"]),
        ("SEPTEMBER", ["September chat"]),
        ("AUGUST", ["August chat"]),
        ("SEPTEMBER 2025", ["Last year"])], g
    assert _group([], [], now) == []
    print("_group: RUNNING, Today / 7 days / 30 days, then per month (+year when not this year)  OK")


def _history(n, now):
    titles = ["Build a coffee website", "Plant shop site", "Fix the login bug", "Tax return folder"]
    return [NS(id=f"h{i}", title=titles[i % 4] + f" {i}", preview=f"preview {i}", mtime=now - 3600 * (i + 1))
            for i in range(n)]


def test_rebuild_keeps_full_list():
    """REGRESSION: selecting a chat rebuilt the (already visible) rail and squeezed the list into a
    40px scroll area — 'only one chat left'. New rows measured 0 height until a queued show event."""
    rail, now = ConversationRail(), time.time()
    hist = _history(40, now)
    rail.update_sessions([], None, hist)
    pump(200)                                                        # rail is now really visible
    assert rail.isVisible()
    full_h, full_scroll = rail.height(), rail._scroll.height()
    assert full_scroll > 200, full_scroll
    picked = NS(key="s1", status="done", started_at=hist[5].mtime, title=hist[5].title, prompt="p")
    rail.update_sessions([picked], "s1", [h for h in hist if h.id != "h5"])   # what selecting does
    pump(200)
    assert len(rail._rows) == 40, len(rail._rows)
    assert rail._scroll.height() == full_scroll, f"list squeezed: {rail._scroll.height()} vs {full_scroll}"
    assert rail.height() == full_h == rail.sizeHint().height()
    print(f"rebuild after selecting: list keeps {full_scroll}px, all 40 rows, rail fits  OK")


def test_search():
    rail, now = ConversationRail(), time.time()
    hist = _history(12, now)
    hist.append(NS(id="old", title="Ancient plant shop chat", preview="", mtime=now - 60 * 86400))
    live = NS(key="s9", status="working", started_at=now - 30, title="Deploy", prompt="ship the plant shop")
    rail.update_sessions([live], None, hist)
    pump(200)
    top, full_h = rail.y(), rail.height()

    rail._search.setText("plant shop"); pump(100)
    titles = [r.findChildren(QLabel)[1].text() for r in rail._rows]     # [dot, title, sub]
    assert rail._groups[0] == ("RUNNING", 1), "a running chat matching by its prompt stays visible"
    d = datetime.fromtimestamp(now - 60 * 86400)
    month = d.strftime("%B").upper() + ("" if d.year == datetime.fromtimestamp(now).year else f" {d.year}")
    assert (month, 1) in rail._groups, f"a 60-day-old match sits in its month group: {rail._groups}"
    assert len(rail._rows) == 1 + 3 + 1 and all("Plant shop" in t or "Ancient" in t or t == "Deploy"
                                                for t in titles), titles
    assert rail.height() < full_h and rail.height() == rail.sizeHint().height(), "rail shrinks to results"
    assert rail.y() == top, "top stays put while typing — the search box doesn't jump"

    rail._search.setText("PREVIEW 3"); pump(100)                     # case-insensitive, matches preview
    assert len(rail._rows) == 1, len(rail._rows)
    rail._search.setText("nothing like this"); pump(100)
    assert rail._rows == [] and rail._groups == []
    rail._search.setText(""); pump(100)
    assert len(rail._rows) == 14 and rail.height() == full_h, "cleared → the full list again"
    print("search: titles + previews + running prompts, older matches, shrinks, top fixed, clears  OK")


def test_rail_rows():
    rail, now = ConversationRail(), time.time()
    running = NS(key="r", status="working", started_at=now - 60, title="Running one")
    recent = [NS(id="a", title="Past A", mtime=now - 120), NS(id="b", title="Past B", mtime=now - 3 * 86400)]
    rail.update_sessions([running], None, recent)
    assert rail._groups[0] == ("RUNNING", 1) and len(rail._rows) == 3, rail._groups
    hits_s, hits_r = [], []
    rail.session_clicked.connect(hits_s.append)
    rail.recent_clicked.connect(hits_r.append)
    rail._rows[0].clicked.emit()
    rail._rows[1].clicked.emit()
    assert hits_s == ["r"] and hits_r == ["a"], (hits_s, hits_r)
    rail.update_sessions([], None, [])
    assert rail._groups == [] and rail._rows == []
    print("rail: groups render, rows emit session/recent clicks, empty state  OK")


class _MW:
    pass


def test_select_past_chat_shows_it_in_notch():
    root = Path(tempfile.mkdtemp())
    asess._projects_root = lambda: root
    t0 = time.time() - 3 * 86400                                    # a chat from 3 days ago
    _write_lines(root / WS_DIR / "sid-fern.jsonl", _fern_transcript(t0), t0 + 130)
    page = ap.AssistantPage(_MW())
    page._sessions = {}
    page._recent = [NS(id="sid-fern", title="Build me a site", preview="Build me a site", cwd=WS,
                       mtime=t0 + 130, project="ws")]
    page._refresh_rail()
    assert page._rail._groups == [("PREVIOUS 7 DAYS", 1)], page._rail._groups

    page._rail._rows[0].clicked.emit()                              # the REAL row click
    for _ in range(100):                                            # real thread → queued signal
        pump(20)
        if page._sessions:
            break
    assert len(page._sessions) == 1, "transcript never loaded"
    sess = page._sessions[page._focused_key]
    assert sess.resume == "sid-fern" and not sess.running and sess.status == "done"
    assert sess.feed_lines == ["▸ Build me a site", "Writing index.html", "Done — built it.",
                               "▸ make it blue", "Editing style.css", "Made it blue."], sess.feed_lines
    assert sess.result_text == "Made it blue." and sess.started_at == t0 + 130
    assert page._rail._groups == [("PREVIOUS 7 DAYS", 1)], "viewing must NOT bump it to Today"

    ov = page._overlay
    assert ov._minimized is False, "selecting a conversation must OPEN the notch on it"
    assert ov._transcript.text() == "Build me a site"
    assert "Made it blue." in ov._done_label.text(), ov._done_label.text()
    details = ov._details_feed.toPlainText()                          # the rich Details feed
    assert "Wrote index.html" in details and "Edited style.css" in details and "Made it blue." in details, details
    assert ov._timer_label.text() == "0:30", f"timer must be the real duration, got {ov._timer_label.text()!r}"
    print("past chat: row click → transcript loaded → notch OPEN showing it, 0:30, stays in its group  OK")

    calls = []
    page._start_worker = lambda s, rid: calls.append((s.key, rid))   # no real claude
    page._on_voice_decided({"intent": "FOLLOW_UP", "idx": None, "task": "add a pricing section",
                            "candidates": [], "focused_key": page._focused_key})
    assert calls == [(sess.key, "sid-fern")] and len(page._sessions) == 1, calls
    print("then speak → continues that same claude session, no duplicate  OK")


def test_select_running_chat_opens_live_view():
    page = ap.AssistantPage(_MW())
    s = ap.AgentSession("s900", WS, "build a thing", "Build Thing", True, "opus", "high")
    s.started_at = time.time() - 65
    s.feed_lines = ["▸ build a thing", "Writing app.py"]
    page._sessions = {s.key: s}
    page._refresh_rail()
    page._rail._rows[0].clicked.emit()
    ov = page._overlay
    assert page._focused_key == "s900" and ov._minimized is False and ov._state == "working"
    assert ov._timer_label.text() in ("1:05", "1:06"), ov._timer_label.text()
    print("running chat: row click → notch OPEN on the live view, timer from the turn's start  OK")


def test_continued_chats_survive_restart():
    """A chat continued in another project folder must still be on the rail after a restart: the
    first run backfills the ones from before (SDK + Claude Code/Desktop turns, outside the workspace),
    later ones are remembered when they finish. SDK-only leftovers (router/tests) stay hidden."""
    root = Path(tempfile.mkdtemp())
    store = root / "assistant_sessions.json"
    asess._projects_root, asess._touched_path = (lambda: root), (lambda: store)
    now = time.time()

    def msg(cwd, entry, text, ts):
        return {"type": "user", "cwd": cwd, "entrypoint": entry, "timestamp": _iso(ts),
                "message": {"role": "user", "content": text}}
    _write_lines(root / WS_DIR / "sid-ws.jsonl", [msg(WS, "sdk-py", "Write an essay", now - 7200)], now - 7200)
    _write_lines(root / "-tmp-proj-crypto" / "sid-crypto.jsonl",
                 [msg("/tmp/proj/crypto", "cli", "Build a trading bot", now - 3600),
                  msg("/tmp/proj/crypto", "sdk-py", "also add stop-losses", now - 3500)], now - 3500)
    _write_lines(root / "-tmp-dev" / "sid-router.jsonl",
                 [msg("/tmp/dev", "sdk-py", "hello", now - 60)], now - 60)

    assert [s.id for s in asess.remembered_sessions(WS)] == ["sid-crypto"] and store.exists()
    _write_lines(root / "-tmp-proj-other" / "sid-other.jsonl",
                 [msg("/tmp/proj/other", "claude-desktop", "Fix the site header", now - 30)], now - 30)
    asess.remember_session("sid-other", "/tmp/proj/other")                 # a run just finished there
    assert [s.id for s in asess.remembered_sessions(WS)] == ["sid-other", "sid-crypto"]

    old_ws, ap.DEFAULT_WORKSPACE = ap.DEFAULT_WORKSPACE, WS
    try:
        page = ap.AssistantPage(_MW())                                      # = the app after a restart
        page._recent = []
        _REAL_LOAD_RECENT(page)
        deadline = time.time() + 5
        while not page._recent and time.time() < deadline:
            pump(20)
        ids = [r.id for r in page._recent]
        assert ids == ["sid-other", "sid-crypto", "sid-ws"], ids            # newest first, no leftovers
        page.shutdown()
    finally:
        ap.DEFAULT_WORKSPACE = old_ws
    print("continued chats: backfilled + remembered, back on the rail after a restart  OK")


def test_idle_rail_shown_before_any_request():
    """At launch, BEFORE any request, the rail (left) must exist so you can hover/click it open. A bad
    edit once moved this startup code into shutdown(), so nothing appeared until the first task."""
    calls = []
    real_idle = ap.AssistantPage.__dict__.get("_show_idle_overlays")
    ap.AssistantPage._show_idle_overlays = lambda self: calls.append(self)
    try:
        page = ap.AssistantPage(_MW())
        assert page._last_notified_key is None              # the rest of __init__ ran too
        deadline = time.time() + 3
        while not calls and time.time() < deadline:
            pump(50)
        assert calls == [page], "idle overlays never scheduled at startup"
    finally:
        ap.AssistantPage._show_idle_overlays = real_idle
    page._sessions, page._recent = {}, []
    page._refresh_rail()                                    # what the idle timer does for the rail
    assert page._rail is not None and page._rail.isVisible(), "no rail at idle"
    page._rail.hide()
    page.shutdown()
    print("idle: rail scheduled + shown at launch, before any request  OK")


def test_quit_mid_task_stops_threads():
    """Quitting while an agent runs must stop its thread first — else Qt aborts on quit
    ("QThread: Destroyed while thread is still running")."""
    from PySide6.QtCore import QThread
    page = ap.AssistantPage(_MW())
    worker = NS(cancelled=False)
    worker.cancel = lambda: setattr(worker, "cancelled", True)
    th = QThread(page)
    th.start()                                       # an event-loop thread, like a live session's
    page._sessions = {"k": NS(worker=worker, thread=th)}
    page.shutdown()
    assert worker.cancelled and th.isFinished() and page._loader_thread.isFinished()
    print("quit mid-task: agent cancelled + every thread stopped before teardown  OK")


if __name__ == "__main__":
    test_fast_path()
    test_load_transcript()
    test_group()
    test_rail_rows()
    test_rebuild_keeps_full_list()
    test_search()
    test_select_past_chat_shows_it_in_notch()
    test_select_running_chat_opens_live_view()
    test_continued_chats_survive_restart()
    test_idle_rail_shown_before_any_request()
    test_quit_mid_task_stops_threads()
    print("test_rail_recent OK")
