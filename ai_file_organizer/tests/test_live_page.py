"""Filect Assistant (hands-free) — the router's IGNORE, the hands-free path through the Agents page, and
the new Assistant tab. No mic, no real agents: a fake listener and fake router.
  · IGNORE is accepted ONLY for hands-free; fn + ⌃ routing is unchanged (its fakes still take 3 args)
  · hands-free: ignored speech never opens the notch or runs anything; a routing failure runs NOTHING
    (fn + ⌃ still falls back to a new chat); a real request opens the card, runs, and is reported back
  · the tab: Start/Stop, rows for what it heard and what happened, the chime only for real requests,
    learning your voice level, settings saved (stubbed), the notch light, opening a chat from a row
Your settings.json is never written.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_live_page.py"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
saves = []
settings._save_config = lambda: saves.append(1)           # NEVER write the user's settings.json
from app.core import intent_router as ir  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
from app.ui import live_page as lp  # noqa: E402

ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None
ap.AssistantPage._load_recent = lambda self: None
tracked, chimes = [], []
lp._track = lambda ev, **k: tracked.append(ev)
lp._chime = lambda name="Tink": chimes.append(name)
settings.live_send_after_sec, settings.live_sensitivity = 2.0, "medium"
settings.live_chime, settings.live_noise_filter = True, True


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


class _MW:
    def __init__(self):
        self.nav = []

    def _on_nav_clicked(self, i):
        self.nav.append(i)


class _Router:
    def __init__(self, intent="NEW", fail=False):
        self.intent, self.fail, self.calls, self.asked = intent, fail, [], []

    def classify(self, text, focused, cands, live=False, asked=None):
        self.calls.append(live)
        self.asked.append(asked)
        if self.fail:
            raise RuntimeError("router down")
        return {"intent": self.intent, "index": None, "task": text}


def _with_router(router):
    real = (ir.IntentRouter.get, ir.classify_with_codex)
    ir.IntentRouter.get = classmethod(lambda cls: router)

    def codex(text, f, c, live=False):
        raise RuntimeError("codex unavailable too")
    ir.classify_with_codex = codex
    return real


def _restore(real):
    ir.IntentRouter.get, ir.classify_with_codex = real


# ---- the router -----------------------------------------------------------------------------------
def test_ignore_only_for_hands_free():
    assert ir._parse('{"intent":"IGNORE","index":null,"task":"x"}', "x", allow_ignore=True)["intent"] == "IGNORE"
    assert ir._parse('{"intent":"IGNORE","index":null,"task":"x"}', "x")["intent"] == "NEW", "fn + ⌃: never IGNORE"
    assert "HANDS-FREE" in ir._build_prompt("hi", None, [], live=True)
    assert "HANDS-FREE" not in ir._build_prompt("hi", None, [])
    print("router: IGNORE only for hands-free; fn + ⌃ prompt unchanged  OK")


# ---- the hands-free path through the Agents page ---------------------------------------------------
def _page():
    page = ap.AssistantPage(_MW())
    page._primary = "claude"
    notch, runs, reports = [], [], []
    page._notch = lambda m, *a: notch.append(m)

    def fake_run(prompt, bypass=None, resume=None, cwd=None, images=None, agent=None):
        runs.append(prompt)
        if resume:                                   # (the real one reuses that chat's session)
            return
        s = ap.AgentSession(f"k{len(runs)}", "/tmp", prompt, "Coffee Shop Site", True, "opus", "high")
        s.session_id = f"sid{len(runs)}"
        page._sessions[s.key] = s
        page._focused_key = s.key
    page.run_task = fake_run
    page.live_routed.connect(lambda tag, o, t, k: reports.append((tag, o, t, k)))
    return page, notch, runs, reports


def test_hands_free_routing():
    page, notch, runs, reports = _page()
    router = _Router("IGNORE")
    real = _with_router(router)
    try:
        page.run_voice_task("yeah I'll call you back in a minute", source="live", tag=7)
        assert _pump(lambda: reports, 3)
        assert reports == [(7, "ignored", "", "")] and runs == [] and "present" not in notch, (reports, notch)
        assert router.calls == [True], "hands-free asks with live=True"

        router.intent = "NEW"
        page.run_voice_task("make a landing page for my coffee shop", source="live", tag=8)
        assert _pump(lambda: len(reports) == 2, 3)
        assert reports[-1] == (8, "new", "Coffee Shop Site", "k1"), reports
        assert runs == ["make a landing page for my coffee shop"] and "present" in notch

        router.intent = "FOLLOW_UP"
        page.run_voice_task("make the header darker", source="live", tag=9)
        assert _pump(lambda: len(reports) == 3, 3)
        assert reports[-1][:2] == (9, "continued") and reports[-1][3] == "k1"

        router.fail = True                                    # Claude AND Codex can't route
        page.run_voice_task("delete my old screenshots", source="live", tag=10)
        assert _pump(lambda: len(reports) == 4, 3)
        assert reports[-1] == (10, "failed", "", "") and len(runs) == 2, "hands-free never guesses"
        page._focused_key = None                               # (so fn + ⌃'s fallback is a NEW chat)

        n_present = notch.count("present")                     # fn + ⌃ is unchanged:
        page.run_voice_task("open my notes")                   #   the card opens at once,
        assert notch.count("present") == n_present + 1
        assert _pump(lambda: len(runs) == 3, 3), "a router failure still falls back to running it"
        assert router.calls[-1] is False and len(reports) == 4, "no live flag, no live report"
    finally:
        _restore(real)
    print("hands-free: ignored → nothing (no card); request → card + run + reported; router down → "
          "nothing; fn + ⌃ unchanged  OK")


def test_esc_while_the_router_checks():
    """Esc after the transcript, while the router is still deciding: the request is dropped — it never
    runs — and reported as cancelled."""
    page, notch, runs, reports = _page()

    class Slow(_Router):
        def classify(self, text, focused, cands, live=False):
            time.sleep(0.6)
            return {"intent": "NEW", "index": None, "task": text}
    real = _with_router(Slow())
    try:
        page.run_voice_task("delete all my screenshots", source="live", tag=21)
        assert page.cancel_live(21) is True
        assert reports == [(21, "cancelled", "", "")]
        _pump(lambda: False, 1.2)                          # the router answers meanwhile
        assert runs == [] and len(reports) == 1, (runs, reports)
        assert page.cancel_live(21) is False, "nothing left to cancel"
        page.run_voice_task("open my notes", source="live", tag=22)   # the next one runs normally
        assert _pump(lambda: runs, 3) and reports[-1][:2] == (22, "new")
    finally:
        _restore(real)
    print("Esc while the router checks: dropped, never runs, reported cancelled; the next runs  OK")


def test_note_under_the_notch():
    from app.ui.claude_overlay import ClaudeOverlay
    ov = ClaudeOverlay()
    h = ov._handle
    h.resize(150, 34)
    blank = h.grab().toImage()
    ov.flash_note("Cancelled", ms=150)
    assert h._note == "Cancelled"
    img = h.grab().toImage()
    drawn = sum(1 for x in range(0, 150, 3) for y in range(0, 34, 3) if img.pixelColor(x, y).alpha() > 0)
    assert drawn > 20, drawn
    assert _pump(lambda: h._note == "", 1), "it goes away by itself"
    ov.set_listening("listening")
    ov.flash_note("Not a request")
    h.grab()
    ov.set_listening("")
    print(f"note under the notch: drawn ({drawn} px sampled), clears itself  OK")


def test_answers_to_the_agent():
    """The agent waits for you: an approval → a spoken "yes"/"no" answers it right away (no router);
    a question its last answer ended with → the router gets that question, so "yes" is a reply, not filler."""
    import time as _t
    page, notch, runs, reports = _page()
    decided = []
    page._decide = lambda ok: decided.append(ok)
    s = ap.AgentSession("w1", "/tmp", "build a site", "Coffee Site", True, "opus", "high")
    page._sessions["w1"], page._focused_key = s, "w1"
    assert page.live_answer_context() is None, "working, nothing asked"
    s.status, s.approval_req = "needs_you", object()
    assert page.live_answer_context()["kind"] == "approval"
    router = _Router("NEW")
    real = _with_router(router)
    try:
        page.run_voice_task("Yes, go ahead.", source="live", tag=31)
        assert decided == [True] and reports[-1] == (31, "approved", "Coffee Site", "w1") and router.calls == []
        s.approval_req = object()
        page.run_voice_task("No, don't.", source="live", tag=32)
        assert decided == [True, False] and reports[-1][1] == "denied"
        # a question at the end of its answer
        s.status, s.approval_req = "done", None
        s.result_text = "I built the page and started it at localhost:4173.\n\nShould I also add a contact form?"
        s.duration = 30
        ctx = page.live_answer_context()
        assert ctx["kind"] == "question" and ctx["question"] == "Should I also add a contact form?", ctx
        for tail, want in (("Should I create it? (yes/no)", "Should I create it?"),
                           ("Want me to deploy it?\n\nLet me know.", "Want me to deploy it?"),
                           ("Should I create a folder called “yes test” inside the “Filect test” folder on your Desktop? "
                            "I’ll wait for your answer before creating it.",              # Codex, his test
                            "Should I create a folder called “yes test” inside the “Filect test” folder on your Desktop?"),
                           ("Done. Created the folder.", ""),
                           ("Done — what a day? I created every folder. Then I tested every page. All good.", "")):
            assert ap._last_question(tail) == want, (tail, ap._last_question(tail))
        page.run_voice_task("Yes.", source="live", tag=33)
        assert _pump(lambda: len(reports) == 3, 3)
        assert router.asked[-1] == "Should I also add a contact form?", router.asked
        s.started_at = _t.time() - page.ANSWER_WINDOW_SEC - 60          # long ago → no longer waiting
        assert page.live_answer_context() is None
        page.run_voice_task("Open my notes.", source="live", tag=34)
        assert _pump(lambda: len(reports) == 4, 3) and router.asked[-1] is None
        page.run_voice_task("open my notes")                            # fn + ⌃: never gets `asked`
        assert _pump(lambda: len(router.asked) == 3, 3) and router.asked[-1] is None and router.calls[-1] is False
    finally:
        _restore(real)
    print("approval + 'yes'/'no' → answered directly; a question it asked → passed to the router; fn + ⌃ unchanged  OK")


def test_fn_ctrl_fakes_with_three_args_still_work():
    page, notch, runs, reports = _page()

    class Old:
        def classify(self, text, focused, cands):             # an fn + ⌃ fake/caller with no `live`
            return {"intent": "NEW", "index": None, "task": text}
    real = _with_router(Old())
    try:
        page.run_voice_task("what is 2 plus 2")
        assert _pump(lambda: runs, 3), "fn + ⌃ never passes the new keyword"
    finally:
        _restore(real)
    print("fn + ⌃ calls the router exactly as before  OK")


# ---- the Assistant tab ------------------------------------------------------------------------------
class FakeListener(QObject):
    esc_late = Signal()
    state_changed = Signal(str, str)
    level = Signal(float)
    countdown = Signal(float)
    utterance_started = Signal(int)
    utterance_ended = Signal(int)
    heard = Signal(int, str)
    dropped = Signal(int, str, str)
    usage = Signal(float)

    def __init__(self):
        super().__init__()
        self.on, self.learned, self.applied, self.probe = False, [], 0, None

    def set_answer_probe(self, fn):
        self.probe = fn

    def start(self):
        self.on = True
        self.state_changed.emit("starting", "Starting the microphone…")

    def stop(self):
        self.on = False
        self.state_changed.emit("off", "")

    def learn(self, uid):
        self.learned.append(uid)

    def apply_settings(self):
        self.applied += 1

    def used_today(self):
        return 125.0


class FakeOverlay:
    def __init__(self):
        self.lights, self.notes = [], []

    def set_listening(self, m):
        self.lights.append(m)

    def flash_note(self, text, color="#B8B8C4", ms=2500):
        self.notes.append(text)


class FakeAP(QObject):
    live_routed = Signal(object, str, str, str)

    def __init__(self):
        super().__init__()
        self.runs, self.focused, self.ov = [], [], FakeOverlay()
        self._sessions, self._overlay = {"k1": object()}, None

    def run_voice_task(self, text, source="voice", images=None, tag=None):
        self.runs.append((text, source, tag))

    def cancel_live(self, tag):
        self.live_routed.emit(tag, "cancelled", "", "")
        return True

    def _ensure_notch(self):
        self._overlay = self.ov
        return self.ov

    def live_answer_context(self):
        return None

    def _focus(self, key):
        self.focused.append(key)


class _Btn:
    def __init__(self):
        self.checked = False

    def setChecked(self, v):
        self.checked = v


class FakeGroup:
    def __init__(self):
        self.b = _Btn()

    def button(self, i):
        return self.b if i == 5 else None


def _tab():
    mw = _MW()
    mw.assistant_page, mw.nav_button_group = FakeAP(), FakeGroup()
    L = FakeListener()
    page = lp.LivePage(mw, listener=L)
    return page, L, mw


def test_tab_start_stop_and_rows():
    page, L, mw = _tab()
    tracked.clear(); chimes.clear()
    assert page.start_btn.text() == "Start Filect Assistant" and "≈ 2 min" in page.usage_label.text(), page.usage_label.text()
    page.start_btn.click()
    assert L.on and page.start_btn.text() == "Stop" and tracked[-1] == "live_started"
    assert L.probe is not None, "the tab tells the listener when the agent waits for your answer"
    L.state_changed.emit("listening", "")
    assert page.status.text() == "Listening — just talk" and mw.assistant_page.ov.lights[-1] == "listening"
    L.state_changed.emit("hearing", "")
    assert mw.assistant_page.ov.lights[-1] == "hearing"
    L.state_changed.emit("countdown", "")
    L.countdown.emit(1.4)
    assert page.status.text() == "Sending in 1.4 s · Esc cancels"
    L.state_changed.emit("paused", "Paused while zoom.us uses the mic")
    assert page.status.text() == "Paused while zoom.us uses the mic" and mw.assistant_page.ov.lights[-1] == "paused"

    # a request: ended → transcribing → heard → run → reported
    L.utterance_ended.emit(1)
    assert page._rows[1].outcome.text() == "Transcribing…" and page._empty.isHidden()
    L.heard.emit(1, "make a landing page for my coffee shop")
    assert mw.assistant_page.runs == [("make a landing page for my coffee shop", "live", 1)]
    assert page._rows[1].text.text() == "“make a landing page for my coffee shop”"
    mw.assistant_page.live_routed.emit(1, "new", "Coffee Shop Site", "k1")
    assert page._rows[1].outcome.text() == "Started “Coffee Shop Site”  →" and page._rows[1].key == "k1"
    assert L.learned == [1] and chimes == ["Tink"] and "live_task" in tracked

    # not a request
    L.utterance_ended.emit(2)
    L.heard.emit(2, "yeah I'll call you back")
    mw.assistant_page.live_routed.emit(2, "ignored", "", "")
    assert page._rows[2].outcome.text() == "Ignored — not a request" and L.learned == [1] and chimes == ["Tink"]
    assert mw.assistant_page.ov.notes[-1] == "Not a request", "ignored speech gets a note at the notch"

    # router down
    L.heard.emit(3, "rename the folder")
    mw.assistant_page.live_routed.emit(3, "failed", "", "")
    assert page._rows[3].outcome.text().startswith("Couldn't check this one")

    # Esc / a cough / Stop while transcribing
    L.dropped.emit(4, "", "cancelled")
    assert page._rows[4].outcome.text() == "Cancelled (Esc) — not sent"
    assert mw.assistant_page.ov.notes[-1] == "✕  Cancelled" and chimes[-1] == "Pop", "Esc is visible + audible"
    n_notes = len(mw.assistant_page.ov.notes)
    L.dropped.emit(4, "delete everything in downloads", "cancelled")   # the cancelled text arrives later
    assert page._rows[4].text.text() == "“delete everything in downloads”"
    assert len(mw.assistant_page.ov.notes) == n_notes, "one note per cancel, not two"
    # a late Esc while the router is still checking one
    L.heard.emit(11, "rename my project folder")
    assert page._rows[11].outcome.text() == "Checking…"
    L.esc_late.emit()
    assert page._rows[11].outcome.text() == "Cancelled (Esc) — not sent" and mw.assistant_page.ov.notes[-1] == "✕  Cancelled"
    L.dropped.emit(5, "", "too short")
    assert 5 not in page._rows, "a cough doesn't get a row"
    L.utterance_ended.emit(6)
    L.dropped.emit(6, "", "stopped")
    assert page._rows[6].outcome.text() == "Not sent — you clicked Stop"

    # "yes" to a waiting approval
    L.heard.emit(12, "yes go ahead")
    mw.assistant_page.live_routed.emit(12, "approved", "Coffee Shop Site", "k1")
    assert page._rows[12].outcome.text() == "Approved — “Coffee Shop Site”  →" and mw.assistant_page.ov.notes[-1] == "✓  Approved"
    assert 12 in L.learned
    # chime off → no chime
    settings.live_chime = False
    n_ch = len(chimes)
    L.heard.emit(7, "open my notes")
    mw.assistant_page.live_routed.emit(7, "continued", "Notes", "k1")
    assert len(chimes) == n_ch and page._rows[7].outcome.text() == "Continued “Notes”  →"
    settings.live_chime = True

    # clicking a row opens that chat in Agents
    page._open_chat(page._rows[1].key)
    assert mw.nav == [5] and mw.nav_button_group.b.checked and mw.assistant_page.focused == ["k1"]

    # newest first, capped
    assert page._rows_box.itemAt(0).widget() is page._rows[7]
    for i in range(100, 100 + lp.MAX_ROWS + 5):
        L.dropped.emit(i, "", "cancelled")
    assert len(page._rows) == lp.MAX_ROWS
    page.clear_activity()
    assert not page._rows and not page._empty.isHidden()

    page.start_btn.click()
    assert not L.on and page.start_btn.text() == "Start Filect Assistant" and tracked[-1] == "live_stopped"
    assert mw.assistant_page.ov.lights[-1] == ""
    print("tab: Start/Stop, status + notch light, rows (started / ignored / failed / cancelled / stopped), "
          "chime only for real requests, learns your level, opens the chat, capped list  OK")


def test_tab_settings():
    page, L, mw = _tab()
    saves.clear()
    page.wait_slider.setValue(7)
    assert settings.live_send_after_sec == 3.5 and page._wait_val.text() == "3.5 s" and saves and L.applied
    page.sens_btns["high"].click()
    assert settings.live_sensitivity == "high" and page.sens_btns["high"].isChecked()
    page.noise_toggle.click()
    assert settings.live_noise_filter is False and page.noise_toggle.text() == "Off"
    page.chime_toggle.click()
    assert settings.live_chime is False
    assert not page.speak_toggle.isEnabled(), "speaking back is phase 2"
    page.noise_toggle.click(); page.chime_toggle.click(); page.sens_btns["medium"].click(); page.wait_slider.setValue(4)
    assert (settings.live_noise_filter, settings.live_chime, settings.live_sensitivity,
            settings.live_send_after_sec) == (True, True, "medium", 2.0)
    for theme in ("light", "dark"):
        page.apply_theme(theme)                                   # re-themes without errors
    print("tab settings: wait / sensitivity / noise filter / chime saved (stubbed) and applied live  OK")


def test_settings_load_validation():
    data = {"live_send_after_sec": 99, "live_sensitivity": "loud", "live_chime": "yes", "live_noise_filter": False}
    import json
    import tempfile
    d = tempfile.mkdtemp()
    p = Path(d) / "settings.json"
    p.write_text(json.dumps(data))
    real = type(settings).get_app_data_dir
    try:
        type(settings).get_app_data_dir = lambda self: Path(d)
        s2 = type(settings)()
    finally:
        type(settings).get_app_data_dir = real
    assert s2.live_send_after_sec == 10.0 and s2.live_sensitivity == "medium" and s2.live_chime is True
    assert s2.live_noise_filter is False
    print("settings: out-of-range / bad values are clamped or ignored on load  OK")


def test_real_wiring_tab_to_agents_page():
    """The real Agents page + the real tab: heard → run_voice_task(live) → router → live_routed (4 args)
    → the row. (A fake listener and router; run_task is faked so no agent runs.)"""
    page_ap, notch, runs, reports = _page()
    mw = _MW()
    mw.assistant_page, mw.nav_button_group = page_ap, FakeGroup()
    L = FakeListener()
    tab = lp.LivePage(mw, listener=L)
    real = _with_router(_Router("NEW"))
    try:
        tab.start()
        L.utterance_ended.emit(1)
        L.heard.emit(1, "summarize my last three emails")
        assert _pump(lambda: tab._rows[1].outcome.text().startswith("Started"), 3), tab._rows[1].outcome.text()
        assert runs == ["summarize my last three emails"] and tab._rows[1].key == "k1"
    finally:
        _restore(real)
        tab.stop()
    print("real wiring: tab → Agents page (live) → router → the row says what started  OK")


if __name__ == "__main__":
    test_ignore_only_for_hands_free()
    test_hands_free_routing()
    test_fn_ctrl_fakes_with_three_args_still_work()
    test_esc_while_the_router_checks()
    test_answers_to_the_agent()
    test_note_under_the_notch()
    test_tab_start_stop_and_rows()
    test_tab_settings()
    test_settings_load_validation()
    test_real_wiring_tab_to_agents_page()
    print("test_live_page OK")
