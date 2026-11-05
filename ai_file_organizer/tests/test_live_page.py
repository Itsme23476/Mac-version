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
lp._chime = lambda: chimes.append(1)
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
        self.intent, self.fail, self.calls = intent, fail, []

    def classify(self, text, focused, cands, live=False):
        self.calls.append(live)
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
        self.on, self.learned, self.applied = False, [], 0

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
        self.lights = []

    def set_listening(self, m):
        self.lights.append(m)


class FakeAP(QObject):
    live_routed = Signal(object, str, str, str)

    def __init__(self):
        super().__init__()
        self.runs, self.focused, self.ov = [], [], FakeOverlay()
        self._sessions, self._overlay = {"k1": object()}, None

    def run_voice_task(self, text, source="voice", images=None, tag=None):
        self.runs.append((text, source, tag))

    def _ensure_notch(self):
        self._overlay = self.ov
        return self.ov

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
    assert L.learned == [1] and chimes == [1] and "live_task" in tracked

    # not a request
    L.utterance_ended.emit(2)
    L.heard.emit(2, "yeah I'll call you back")
    mw.assistant_page.live_routed.emit(2, "ignored", "", "")
    assert page._rows[2].outcome.text() == "Ignored — not a request" and L.learned == [1] and chimes == [1]

    # router down
    L.heard.emit(3, "rename the folder")
    mw.assistant_page.live_routed.emit(3, "failed", "", "")
    assert page._rows[3].outcome.text().startswith("Couldn't check this one")

    # Esc / a cough / Stop while transcribing
    L.dropped.emit(4, "", "cancelled")
    assert page._rows[4].outcome.text() == "Cancelled (Esc)"
    L.dropped.emit(5, "", "too short")
    assert 5 not in page._rows, "a cough doesn't get a row"
    L.utterance_ended.emit(6)
    L.dropped.emit(6, "", "stopped")
    assert page._rows[6].outcome.text() == "Not sent — you clicked Stop"

    # chime off → no chime
    settings.live_chime = False
    L.heard.emit(7, "open my notes")
    mw.assistant_page.live_routed.emit(7, "continued", "Notes", "k1")
    assert chimes == [1] and page._rows[7].outcome.text() == "Continued “Notes”  →"
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
    test_tab_start_stop_and_rows()
    test_tab_settings()
    test_settings_load_validation()
    test_real_wiring_tab_to_agents_page()
    print("test_live_page OK")
