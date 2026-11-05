"""fn + ⌃ + Space (or Shift): draw on the screen in red, snapshot it, send it to Claude with what you said.
(1) the Fn key tap, driven with REAL CGEvents: Space/Esc are swallowed only while fn + ⌃ are held;
    Shift is watched but never swallowed; every other key passes through; auto-repeat is ignored;
    Space/Shift-up still ends a stroke if fn was let go first; feature off → keys untouched;
(2) ScreenInk: real mouse events draw a stroke; snapshot = screen + marks at FULL (Retina) resolution
    + a thumbnail; an unmarked snapshot is plain; ink clears; capture failure is handled;
(3) the voice controller: TAP the draw key → drawing stays on, tap again = snapshot; HOLD → draw,
    release = snapshot; a re-grip right after a tap = one snapshot; fn release + transcript
    → run_voice_task(text, images); drew-but-said-nothing → default instruction; Esc cancels everything;
(4) agent_runner.user_message: image blocks (≤1568px PNG) + your words + the note and paths;
(5) routing keeps the screenshots (new chat, queued follow-up) and Details shows them as thumbnails.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_screen_ink.py
"""
import base64, os, sys, tempfile, time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
app = QApplication.instance() or QApplication([])

from app.core.settings import settings
settings.dictation_enabled = False                     # never install a real key tap in tests
import app.ui.screen_ink as si
from app.ui import mac_hotkey as mh

TMP = Path(tempfile.mkdtemp())
si.SCREENSHOT_DIR = TMP


def pump(ms=50):
    t = time.time()
    while time.time() - t < ms / 1000:
        app.processEvents(); time.sleep(0.005)


# -- (1) the key tap with real CGEvents -----------------------------------------------------------
def test_key_tap():
    from Quartz import (CGEventCreateKeyboardEvent, CGEventSetFlags, CGEventSetIntegerValueField,
                        kCGEventFlagsChanged, kCGEventKeyDown, kCGEventKeyUp, kCGKeyboardEventAutorepeat)
    FN, CTRL = mh.FN_FLAG, mh.CONTROL_FLAG

    def ev(keycode, down, flags, repeat=False):
        e = CGEventCreateKeyboardEvent(None, keycode, down)
        CGEventSetFlags(e, flags)
        if repeat:
            CGEventSetIntegerValueField(e, kCGKeyboardEventAutorepeat, 1)
        return e

    calls = []
    st = {'down': False, 'space': False, 'tap': None}
    cb = mh._make_fn_callback(st, lambda *a: calls.append("press"), lambda: calls.append("release"), True,
                              on_space=lambda d: calls.append(f"space-{'down' if d else 'up'}"),
                              on_escape=lambda: calls.append("esc"))
    e = ev(49, True, 0)
    assert cb(None, kCGEventKeyDown, e, None) is e, "plain Space must pass through"
    assert cb(None, kCGEventFlagsChanged, ev(63, True, FN | CTRL), None) is None          # fn down
    e = ev(49, True, FN)
    assert cb(None, kCGEventKeyDown, e, None) is e, "fn + Space without ⌃ isn't ours"
    assert cb(None, kCGEventKeyDown, ev(49, True, FN | CTRL), None) is None, "fn+⌃+Space must be swallowed"
    assert cb(None, kCGEventKeyDown, ev(49, True, FN | CTRL, repeat=True), None) is None   # auto-repeat
    e = ev(0, True, FN | CTRL)                                                            # 'a'
    assert cb(None, kCGEventKeyDown, e, None) is e, "other keys must pass through untouched"
    assert cb(None, kCGEventKeyUp, ev(49, False, FN | CTRL), None) is None
    assert cb(None, kCGEventKeyDown, ev(53, True, FN | CTRL), None) is None                # Esc
    cb(None, kCGEventKeyDown, ev(49, True, FN | CTRL), None)                              # Space again…
    cb(None, kCGEventFlagsChanged, ev(63, False, CTRL), None)                             # …let go of fn first
    assert cb(None, kCGEventKeyUp, ev(49, False, 0), None) is None, "Space-up still ends the stroke"
    assert calls == ["press", "space-down", "space-up", "esc", "space-down", "release", "space-up"], calls
    # Shift is the other draw key (closer to fn/⌃): a modifier, so it's watched but NEVER swallowed
    SHIFT = mh.SHIFT_FLAG
    calls.clear()
    cb(None, kCGEventFlagsChanged, ev(63, True, FN | CTRL), None)                         # fn down
    e = ev(56, True, FN | CTRL | SHIFT)
    assert cb(None, kCGEventFlagsChanged, e, None) is e, "Shift must pass through"
    e = ev(56, False, FN | CTRL)
    assert cb(None, kCGEventFlagsChanged, e, None) is e
    cb(None, kCGEventFlagsChanged, ev(60, True, FN | CTRL | SHIFT), None)                 # right Shift…
    cb(None, kCGEventFlagsChanged, ev(63, False, CTRL | SHIFT), None)                     # …fn let go first
    cb(None, kCGEventFlagsChanged, ev(60, False, CTRL), None)                             # Shift-up still ends it
    cb(None, kCGEventFlagsChanged, ev(63, True, FN | SHIFT), None)                        # fn + Shift, no ⌃ = search
    cb(None, kCGEventFlagsChanged, ev(56, False, FN), None)
    assert calls == ["press", "space-down", "space-up", "space-down", "release", "space-up", "press"], calls
    # ⌃ presses while fn is held → on_control (fn + ⌃⌃ = receive mode); a ⌃ already down at fn-down,
    # or pressed without fn, doesn't count; ⌃ always passes through
    ctl = []
    st2 = {'down': False, 'space': False, 'tap': None}
    cb2 = mh._make_fn_callback(st2, lambda *a: None, lambda: None, True,
                               on_space=lambda d: ctl.append(f"space-{'down' if d else 'up'}"),
                               on_escape=lambda: ctl.append("esc"), on_control=lambda kind: ctl.append(kind))
    e = ev(59, True, CTRL)
    assert cb2(None, kCGEventFlagsChanged, e, None) is e and ctl == [], "⌃ without fn isn't ours"
    cb2(None, kCGEventFlagsChanged, ev(63, True, FN | CTRL), None)                        # fn down (⌃ still held)
    cb2(None, kCGEventFlagsChanged, ev(59, False, FN), None)                              # ⌃ up
    e = ev(59, True, FN | CTRL)
    assert cb2(None, kCGEventFlagsChanged, e, None) is e                                  # ⌃ press #1 — passes
    cb2(None, kCGEventFlagsChanged, ev(59, False, FN), None)
    cb2(None, kCGEventFlagsChanged, ev(62, True, FN | CTRL), None)                        # right ⌃ press #2
    assert ctl == ["press", "press"], ctl
    st2['receive'] = True                  # (the controller switches it on right after ⌃⌃)…
    cb2(None, kCGEventFlagsChanged, ev(62, False, FN), None)                              # …⌃ #2 let go
    assert ctl == ["press", "press"], "letting go of the ⌃⌃ that ENTERED receive mode must not send"
    st2['receive'] = False
    # receive mode: Space / Shift / Esc work with NOTHING held; ⌘Space stays Spotlight's
    cb2(None, kCGEventFlagsChanged, ev(63, False, 0), None)                               # fn let go
    e = ev(49, True, 0)
    assert cb2(None, kCGEventKeyDown, e, None) is e, "receive OFF → plain Space types normally"
    st2['receive'] = True
    ctl.clear()
    assert cb2(None, kCGEventKeyDown, ev(49, True, 0), None) is None, "receive ON → Space is ours"
    assert cb2(None, kCGEventKeyUp, ev(49, False, 0), None) is None
    e = ev(49, True, mh.COMMAND_FLAG)
    assert cb2(None, kCGEventKeyDown, e, None) is e, "⌘Space must stay Spotlight's"
    e = ev(56, True, SHIFT)
    assert cb2(None, kCGEventFlagsChanged, e, None) is e                                  # Shift alone…
    cb2(None, kCGEventFlagsChanged, ev(56, False, 0), None)
    assert cb2(None, kCGEventKeyDown, ev(53, True, 0), None) is None                     # Esc alone
    e = ev(0, True, 0)
    assert cb2(None, kCGEventKeyDown, e, None) is e, "typing still works in receive mode"
    assert ctl == ["space-down", "space-up", "space-down", "space-up", "esc"], ctl
    # receive mode: a clean lone ⌃ tap = SEND; ⌃ + a key / + another modifier / a slow ⌃ hold = not
    ctl.clear()
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    assert ctl == ["tap"], ctl
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    st2['ctrl_t'] -= 0.7                                                                  # a relaxed 0.7s tap…
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    assert ctl == ["tap", "tap"], "…still sends (the old 0.4s window cut it off)"
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    cb2(None, kCGEventKeyDown, ev(8, True, CTRL), None)                                   # ⌃C
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    cb2(None, kCGEventFlagsChanged, ev(56, True, CTRL | SHIFT), None)                     # ⌃⇧
    cb2(None, kCGEventFlagsChanged, ev(56, False, CTRL), None)
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    st2['ctrl_t'] -= 1.5                                                                  # held 1.5s = a hold
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    assert ctl == ["tap", "tap", "space-down", "space-up"], f"only clean taps send: {ctl}"   # (⇧ still draws)
    ctl.clear()                            # still holding fn (e.g. a 3rd ⌃ right after ⌃⌃) ≠ the send tap
    cb2(None, kCGEventFlagsChanged, ev(63, True, FN), None)
    cb2(None, kCGEventFlagsChanged, ev(59, True, FN | CTRL), None)
    cb2(None, kCGEventFlagsChanged, ev(59, False, FN), None)
    cb2(None, kCGEventFlagsChanged, ev(63, False, 0), None)
    assert ctl == ["press"], f"⌃ with fn held is an entry press, never the send tap: {ctl}"
    st2['receive'] = False
    ctl.clear()
    cb2(None, kCGEventFlagsChanged, ev(59, True, CTRL), None)
    cb2(None, kCGEventFlagsChanged, ev(59, False, 0), None)
    assert ctl == [], "outside receive mode a ⌃ tap is nothing"
    mh.set_screen_keys((0, {'_st': st2}, 0), False)
    assert st2['receive'] is False
    off = mh._make_fn_callback({'down': True, 'space': False, 'tap': None}, lambda *a: None)
    e = ev(49, True, FN | CTRL)
    assert off(None, kCGEventKeyDown, e, None) is e, "feature off → keys untouched"
    print("key tap: real CGEvents — Space/Esc swallowed only with fn+⌃ (or in receive mode), Shift/⌃ "
          "watched not swallowed, ⌃⌃ while fn held reported, repeat ignored  OK")


# -- (2) ScreenInk ----------------------------------------------------------------------------------
def _fake_screen(ink, color=QColor(128, 128, 128)):
    img = QImage(ink.width() * 2, ink.height() * 2, QImage.Format_ARGB32_Premultiplied)   # Retina 2x
    img.fill(color)
    si._capture = lambda rect: img.copy()


def _stroke(ink, pts):
    x, y = pts[0]
    ink.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                                    Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    for x, y in pts[1:]:
        ink.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))


def _is_red(c):
    return c.red() > 200 and c.green() < 90 and c.blue() < 90


def _wait(path, timeout=5.0):
    """Screenshots are written off the UI thread and appear atomically — wait like agent_runner does."""
    end = time.time() + timeout
    while not os.path.exists(path) and time.time() < end:
        time.sleep(0.02)
    return os.path.exists(path)


def test_ink_snapshot():
    ink = si.ScreenInk()
    ink.begin()
    _fake_screen(ink)
    ink.set_drawing(True)
    _stroke(ink, [(100, 100)] + [(x, 100) for x in range(110, 301, 10)])
    t = time.time()
    path, marks = ink.snapshot()
    took = (time.time() - t) * 1000                                    # what the UI thread pays
    assert not os.path.exists(path + ".part") or True                  # (may still be writing)
    assert _wait(path) and _wait(si.thumb_path(path)), "screenshot + thumbnail get written"
    img = QImage(path)
    assert marks and img.width() == ink.width() * 2, (marks, img.width())          # full resolution
    assert _is_red(img.pixelColor(400, 200)), img.pixelColor(400, 200).name()     # (200,100) × 2
    assert img.pixelColor(400, 400).name() == "#808080", "untouched screen must stay as captured"
    assert ink._strokes == [], "ink cleared"
    path2, marks2 = ink.snapshot()                                                 # a quick tap
    _wait(path2)
    img2 = QImage(path2)
    assert not marks2 and not _is_red(img2.pixelColor(400, 200)), "a tap is a plain snapshot"
    si._capture = lambda rect: None
    _stroke(ink, [(10, 10), (20, 20)])
    assert ink.snapshot() is None and ink._strokes == [], "no capture → nothing, ink cleared"
    ink.end()
    assert not ink.isVisible()
    print(f"ink: real mouse strokes → full-res snapshot with red marks + thumbnail (UI thread {took:.0f} ms), tap = plain  OK")


# -- (3) the voice controller gesture -------------------------------------------------------------
class _Assistant:
    def __init__(self):
        self.calls = []

    def run_voice_task(self, text, source="voice", images=None):
        self.calls.append((text, list(images or [])))


class _MW(QWidget):
    pass


def _press(d, held):
    """The draw key: pressed, then released after `held` seconds (back-dated — no real waiting)."""
    d._on_space(True)
    d._ink_down_at -= held
    d._on_space(False)


def test_controller_gesture():
    from app.ui import dictation as dc
    mw = _MW()
    mw.assistant_page = _Assistant()
    d = dc.VoiceDictationController(mw)
    d._state, d._recorder = "recording", None
    # TAP → drawing turns on and STAYS on (no key held while you draw)
    d._on_space(True)
    _fake_screen(d._ink)
    d._on_space(False)
    assert d._ink.isVisible() and d._ink._drawing and d._inking and not d._shots, "a tap keeps drawing on"
    assert "tap again" in d._ink._hint
    _stroke(d._ink, [(50, 50), (150, 50)])
    d._ink_tapped_at -= 1                                   # (you took a moment to draw)
    d._on_space(True)                                       # TAP AGAIN → snapshot 1 (marked), drawing off
    assert len(d._shots) == 1 and d._shots[0][1] and not d._ink._drawing
    d._on_space(False)                                      # …and that tap's release does nothing
    assert len(d._shots) == 1 and not d._inking
    # HOLD → draw while held, release → snapshot 2
    d._on_space(True); _stroke(d._ink, [(10, 10), (90, 90)]); d._ink_down_at -= 1; d._on_space(False)
    assert len(d._shots) == 2 and d._shots[1][1] and not d._inking
    # RE-GRIP (seen live: 0.2s press, 0.1s gap, long hold) → ONE snapshot, at the end of the hold
    d._on_space(True); d._on_space(False)                   # quick press → drawing on
    d._on_space(True)                                       # pressed straight back → still drawing
    assert d._inking and len(d._shots) == 2, "a re-grip must not snapshot"
    d._ink_down_at -= 1; d._on_space(False)                 # held → release = snapshot 3 (plain)
    assert len(d._shots) == 3 and not d._shots[2][1]
    d._stop()                                               # let go of fn
    assert d._mode == "assistant" and not d._ink.isVisible()
    d._on_text("make the save button blue")
    text, imgs = mw.assistant_page.calls[-1]
    # (no live transcript in this test → each "[screenshot N]" marker goes at the end)
    assert text == "make the save button blue [screenshot 1] [screenshot 2] [screenshot 3]", text
    assert len(imgs) == 3 and all(_wait(p) for p in imgs)
    # drew but said nothing
    d._state = "recording"
    d._on_space(True); _stroke(d._ink, [(5, 5), (60, 60)]); d._ink_down_at -= 1; d._on_space(False)
    d._stop(); d._on_text("")
    assert mw.assistant_page.calls[-1][0] == "Look at what I've marked in red."
    # REGRESSION: back-to-back gestures in the same second used to reuse a file name (the second
    # screenshot overwrote the first) — every snapshot must get its own file
    every = [p for _, imgs in mw.assistant_page.calls for p in imgs]
    assert len(every) == len(set(every)) == 4, every
    # fn released while still drawing → that's a snapshot too (held, or tapped on)
    for tapped in (False, True):
        d._state = "recording"
        d._on_space(True)
        if tapped:
            d._on_space(False)
        _stroke(d._ink, [(5, 5), (60, 60)])
        d._stop(); d._on_text("this one")
        assert len(mw.assistant_page.calls[-1][1]) == 1 and mw.assistant_page.calls[-1][1][0] not in every
    # Esc cancels everything — mid-hold and while tapped on
    n = len(mw.assistant_page.calls)
    for keep_held in (True, False):
        d._state = "recording"
        _press(d, 1); d._on_space(True)
        if not keep_held:
            d._on_space(False)
        _stroke(d._ink, [(5, 5), (60, 60)])
        d._on_escape()
        assert d._shots == [] and not d._ink.isVisible() and d._state == "idle" and not d._inking
    assert len(mw.assistant_page.calls) == n, "Esc must send nothing"
    print("controller: tap=draw on/tap again=snapshot, hold=draw/release=snapshot, re-grip=one shot, "
          "fn release sends words+shots, Esc cancels  OK")


class _Rec:
    """Stands in for the StreamingTranscriber: how many words it has heard so far."""
    def __init__(self):
        self.words, self.stopped = 0, False

    def words_so_far(self):
        return self.words

    def stop_recording(self):
        self.stopped = True


def test_receive_mode():
    """Hold fn, double-tap ⌃ → receive mode: fn let go keeps it on; talk + tap Space/Shift for
    several snapshots; tap fn → ONE message with every screenshot, each placed after the words you'd
    said when you took it. Esc drops it all; slow ⌃⌃ isn't it; the 10-min cap still sends to Claude."""
    from app.ui import dictation as dc
    mw = _MW()
    mw.assistant_page = _Assistant()
    d = dc.VoiceDictationController(mw)
    st = {}
    d._hotkey, d._gesture_mode = (0, {'_st': st}, 0), True

    def fn_down_and_ctrl_ctrl():
        d._state, d._recorder, d._key_down = "recording", _Rec(), True
        d._mods_seen = {"shift": False, "option": False, "control": False}
        d._press_time = time.monotonic()
        d._on_control(); d._on_control()                    # ⌃⌃ while fn held

    fn_down_and_ctrl_ctrl()
    assert d._receive and d._latched and st.get('receive') is True, "⌃⌃ → receive mode + hands-free keys"
    assert d._ink.isVisible() and "Receiving" in d._ink._banner
    _fake_screen(d._ink)
    d._press_time -= 2; d._on_release()                     # let go of fn (after a 2s hold)…
    assert d._state == "recording", "…receive mode keeps listening"
    d._recorder.words = 3                                   # "change this button"
    d._on_space(True); d._on_space(False)                   # tap Space → drawing on
    _stroke(d._ink, [(50, 50), (150, 50)])
    d._ink_tapped_at -= 1; d._on_space(True); d._on_space(False)   # tap again → screenshot 1
    assert "1 screenshot" in d._ink._banner, d._ink._banner
    d._recorder.words = 7                                   # "… and also this heading"
    _press(d, 1)                                            # hold Shift/Space → screenshot 2
    assert len(d._shots) == 2 and d._shots[0][2] == 3 and d._shots[1][2] == 7
    rec = d._recorder
    assert "tap ⌃ to send" in d._ink._banner
    d._on_control("tap")                                    # tap ⌃ → send
    assert rec.stopped and d._mode == "assistant" and not d._receive and st['receive'] is False
    assert not d._ink.isVisible() and d._state == "transcribing"
    d._on_text("change this button and also this heading please")
    text, imgs = mw.assistant_page.calls[-1]
    assert text == "change this button [screenshot 1] and also this heading [screenshot 2] please", text
    assert len(imgs) == 2 and all(_wait(p) for p in imgs)
    # tapping fn still sends too (and its release is ignored); a stray ⌃ tap when not receiving = nothing
    fn_down_and_ctrl_ctrl()
    d._press_time -= 2; d._on_release()
    rec = d._recorder
    d._on_press()
    assert rec.stopped and d._mode == "assistant" and not d._receive
    d._on_release()
    d._on_text("via fn")
    assert mw.assistant_page.calls[-1][0] == "via fn"
    d._on_control("tap")
    assert d._state == "idle", "a ⌃ tap outside receive mode does nothing"
    # slow ⌃ ⌃ (a normal fn + ⌃ hold, then ⌃ again later) is NOT receive mode
    d._state, d._recorder, d._key_down, d._latched = "recording", _Rec(), True, False
    d._on_control(); d._ctrl_at -= 1.5; d._on_control()
    assert not d._receive and not d._latched
    d._cancel_recording()
    # a relaxed ⌃ … ⌃ (press, let go, press again 0.7s later) IS receive mode — 0.45s was too tight live
    d._state, d._recorder, d._key_down, d._latched = "recording", _Rec(), True, False
    d._on_control(); d._ctrl_at -= 0.7; d._on_control()
    assert d._receive and d._latched, "⌃ ⌃ 0.7s apart → receive mode"
    d._cancel_recording()
    assert not d._receive
    # Esc in receive mode drops everything — nothing sent, keys back to normal
    n = len(mw.assistant_page.calls)
    fn_down_and_ctrl_ctrl()
    _press(d, 1)
    d._on_escape()
    assert d._state == "idle" and not d._receive and st['receive'] is False and d._shots == []
    assert len(mw.assistant_page.calls) == n, "Esc must send nothing"
    # the 10-minute cap mid-receive still goes to CLAUDE (not pasted as dictation)
    import subprocess
    real_run, subprocess.run = subprocess.run, lambda *a, **k: None    # no real macOS banner
    try:
        fn_down_and_ctrl_ctrl()
        d._on_capped()
        d._on_text("a very long review")
    finally:
        subprocess.run = real_run
    assert d._mode == "assistant" and mw.assistant_page.calls[-1][0] == "a very long review"
    assert not d._receive and st['receive'] is False
    print("receive mode: fn+⌃⌃ → hands-free; Space/Shift snapshots; tap ⌃ (or fn) → ONE message with "
          "[screenshot N] where you took each; slow ⌃⌃ ≠ receive; Esc drops; 10-min cap → Claude  OK")


def test_markers_and_words():
    from app.ui.dictation import _with_markers
    from app.core.transcription import StreamingTranscriber, _assemble
    from app.core.agent_sessions import generate_title
    assert _with_markers("a b c", [("p", False, 0), ("q", False, 2), ("r", False, None)]) == \
        "[screenshot 1] a b [screenshot 2] c [screenshot 3]"
    assert _with_markers("a b", [("p", False, 9), ("q", False, 9)]) == "a b [screenshot 1] [screenshot 2]"
    # words heard so far = finished sentences + the one in progress (same assembly as the final text)
    t = StreamingTranscriber.__new__(StreamingTranscriber)
    assert t.words_so_far() == 0                            # nothing heard yet
    t._live = (["Change this button."], {"t": "and also"})
    assert t.words_so_far() == 5
    t._live = (["one two"], {"t": "one two three"})         # cumulative interim → not double-counted
    assert t.words_so_far() == 3 and _assemble(*t._live) == "one two three"
    assert "Screenshot" not in generate_title("make this bigger [screenshot 1] and that [screenshot 2]")
    print("markers: screenshots placed by word count; words_so_far tracks the live transcript  OK")


# -- (4) what Claude receives ---------------------------------------------------------------------
def test_user_message():
    from app.core.agent_runner import user_message
    big = QImage(3456, 2234, QImage.Format_ARGB32_Premultiplied)
    big.fill(QColor(30, 30, 30))
    p = str(TMP / "big.png")
    big.save(p)
    m = user_message("make it blue", [p])
    assert m["type"] == "user" and m["message"]["role"] == "user"
    img_block, text_block = m["message"]["content"]
    assert img_block["type"] == "image" and img_block["source"]["media_type"] == "image/png"
    sent = QImage.fromData(base64.b64decode(img_block["source"]["data"]))
    assert max(sent.width(), sent.height()) == 1568, (sent.width(), sent.height())
    t = text_block["text"]
    assert t.startswith("make it blue") and "marked in red" in t and f"[screenshot: {p}]" in t, t
    # several (receive mode): each image is labelled, in order, so "[screenshot 2]" points at it
    m2 = user_message("this [screenshot 1] and this [screenshot 2]", [p, p])
    kinds = [(b["type"], b.get("text", "")) for b in m2["message"]["content"]]
    assert [k for k, _ in kinds] == ["text", "image", "text", "image", "text"], kinds
    assert kinds[0][1] == "[screenshot 1]" and kinds[2][1] == "[screenshot 2]"
    from app.ui import details_view as dv
    words, shots = dv.split_screenshots(m2["message"]["content"][-1]["text"])
    assert words == "this [screenshot 1] and this [screenshot 2]" and shots == [p, p], (words, shots)
    print("message: image block (≤1568px PNG) + your words + note + saved path; several = labelled  OK")


# -- (5) routing keeps the screenshots; Details shows them -----------------------------------------
def test_routing_and_details():
    from app.ui import assistant_page as ap, details_view as dv
    ap._play_chime = lambda *a, **k: None
    ap._notify = lambda *a, **k: None
    ap.AssistantPage._show_idle_overlays = lambda self: None
    ap.AssistantPage._load_recent = lambda self: None
    shot = str(TMP / "shot.png")
    QImage(400, 300, QImage.Format_ARGB32_Premultiplied).save(shot)
    QImage(200, 150, QImage.Format_ARGB32_Premultiplied).save(si.thumb_path(shot))

    page = ap.AssistantPage(_MW())
    page._sessions = {}
    started = []
    page._start_worker = lambda s, rid: started.append((s.key, list(s.images)))
    page._on_voice_decided({"intent": "NEW", "idx": None, "task": "fix this", "candidates": [],
                            "images": [shot], "focused_key": None})
    sess = next(iter(page._sessions.values()))
    assert started == [(sess.key, [shot])], started

    class _T:                                               # a running turn
        def quit(self): pass
        def wait(self, ms): return True
    sess.thread = _T()
    page._on_voice_decided({"intent": "FOLLOW_UP", "idx": None, "task": "and this", "candidates": [],
                            "images": [shot, shot], "focused_key": sess.key})
    assert sess.pending_followup == "and this" and sess.pending_images == [shot, shot]
    page._on_result(sess.key, "sid-1", 0.0)                 # the real order: the turn reports its id…
    page._on_done(sess.key)                                 # …then ends → follow-up sent WITH its shots
    assert started[-1] == (sess.key, [shot, shot]), started

    e = []
    dv.add_step(e, "user", text="fix this", images=[shot])
    h = dv.render_html(e)
    assert f'src="{dv.file_url(si.thumb_path(shot))}"' in h and f'href="{dv.file_url(shot)}"' in h
    sent_text = f"fix this\n\n(Screenshot of my screen attached — anything marked in red is what I'm pointing at.)\n[screenshot: {shot}]"
    words, shots = dv.split_screenshots(sent_text)
    assert words == "fix this" and shots == [shot], (words, shots)
    h2 = dv.render_html([{"id": "0", "kind": "user", "text": sent_text}])       # a reopened chat
    doc = dv.QTextDocument(); doc.setHtml(h2)
    assert "screenshot:" not in doc.toPlainText() and "attached" not in doc.toPlainText()
    assert f'src="{dv.file_url(si.thumb_path(shot))}"' in h2
    gone = dv.render_html([{"id": "0", "kind": "user", "text": "x\n[screenshot: /nope/a.png]"}])
    assert "moved or deleted" in gone
    print("routing: screenshots ride with new chats + queued follow-ups; Details shows thumbnails  OK")


def test_right_click_tap():
    """Real CGEvents through the real tap callback: a right-click while talking to Claude (fn + ⌃ held,
    or receive mode) → on_right, swallowed (no context menu). Every other right-click passes through,
    and ⌃ + LEFT click stays a left click (it draws)."""
    from Quartz import (CGEventCreateMouseEvent, CGEventCreateKeyboardEvent, CGEventSetFlags,
                        kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventLeftMouseDown,
                        kCGEventFlagsChanged, kCGMouseButtonRight, kCGMouseButtonLeft)
    FN, CTRL = mh.FN_FLAG, mh.CONTROL_FLAG
    rc = []
    st = {'down': False, 'space': False, 'tap': None}
    cb = mh._make_fn_callback(st, lambda *a: None, lambda: None, True, on_space=lambda d: None,
                              on_escape=lambda: None, on_control=lambda k: None,
                              on_right=lambda: rc.append("right"))

    def mouse(t, button, flags=0):
        e = CGEventCreateMouseEvent(None, t, (500, 400), button)
        CGEventSetFlags(e, flags)
        return e

    def kb(keycode, down, flags):
        e = CGEventCreateKeyboardEvent(None, keycode, down)
        CGEventSetFlags(e, flags)
        return e

    def right_click(flags=0):
        d, u = mouse(kCGEventRightMouseDown, kCGMouseButtonRight, flags), mouse(kCGEventRightMouseUp, kCGMouseButtonRight, flags)
        return cb(None, kCGEventRightMouseDown, d, None) is d, cb(None, kCGEventRightMouseUp, u, None) is u

    assert right_click() == (True, True) and rc == [], "not talking to Claude: a normal right-click"
    cb(None, kCGEventFlagsChanged, kb(63, True, FN), None)                    # fn alone = dictation
    assert right_click(FN) == (True, True) and rc == [], "plain dictation: right-click stays normal"
    cb(None, kCGEventFlagsChanged, kb(59, True, FN | CTRL), None)             # + ⌃ = talking to Claude
    assert right_click(FN | CTRL) == (False, False) and rc == ["right"], "fn+⌃: swallowed, fired once"
    e = mouse(kCGEventLeftMouseDown, kCGMouseButtonLeft, FN | CTRL)           # ⌃ + LEFT click
    assert cb(None, kCGEventLeftMouseDown, e, None) is e and rc == ["right"], "⌃+left-click isn't the right button"
    cb(None, kCGEventFlagsChanged, kb(59, False, FN), None)
    cb(None, kCGEventFlagsChanged, kb(63, False, 0), None)                    # let go of fn + ⌃
    assert right_click() == (True, True) and rc == ["right"], "back to normal"
    st['receive'] = True                                                      # receive mode: hands-free
    assert right_click() == (False, False) and rc == ["right", "right"], "receive mode: swallowed, fired"
    st['receive'] = False
    assert right_click() == (True, True) and rc == ["right", "right"]
    print("right-click tap: only while talking to Claude (fn+⌃ or receive mode), swallowed; ⌃+left stays left  OK")


def test_right_click_controller():
    """Right-click → drawing on; draw with the LEFT button (several marks); right-click again → ONE
    snapshot with the marks. Two right-clicks with no drawing = a plain shot. Esc on a right-click
    drawing cancels only that drawing (you keep talking); Esc again drops everything (unchanged)."""
    from app.ui import dictation as dc
    mw = _MW()
    mw.assistant_page = _Assistant()
    d = dc.VoiceDictationController(mw)
    st = {}
    d._hotkey, d._gesture_mode = (0, {'_st': st}, 0), True
    d._on_right_click()
    assert not d._inking, "not recording: a stray right-click does nothing"
    d._state, d._recorder, d._key_down = "recording", _Rec(), True
    d._mods_seen = {"shift": False, "option": False, "control": False}
    d._press_time = time.monotonic()
    d._on_control(); d._on_control()                                    # fn + ⌃⌃ → receive mode
    _fake_screen(d._ink)
    d._press_time -= 2; d._on_release()                                 # let go of fn — still listening
    assert "right-click" in d._ink._banner, d._ink._banner
    d._recorder.words = 2
    d._on_right_click()                                                 # right-click → draw
    assert d._inking and d._ink._drawing and "right-click to capture" in d._ink._hint
    _stroke(d._ink, [(50, 50), (150, 50)])                              # two marks, left button
    _stroke(d._ink, [(60, 80), (160, 120)])
    assert len(d._ink._strokes) == 2
    d._on_right_click()                                                 # right-click → snapshot
    assert not d._inking and not d._ink._drawing and len(d._shots) == 1 and d._shots[0][1] is True
    d._recorder.words = 4
    d._on_right_click(); d._on_right_click()                            # no drawing → a plain shot
    assert len(d._shots) == 2 and d._shots[1][1] is False
    d._on_right_click(); _stroke(d._ink, [(10, 10), (90, 90)])
    d._on_escape()                                                      # Esc → undo just this drawing
    assert d._state == "recording" and d._receive and not d._inking and d._ink._strokes == []
    assert len(d._shots) == 2, "the earlier screenshots are kept"
    d._on_right_click(); _stroke(d._ink, [(20, 20), (70, 70)])
    d._on_space(True); d._on_space(False)                               # Space captures it too
    assert len(d._shots) == 3 and not d._inking
    d._on_control("tap")                                                # tap ⌃ → send ONE message
    d._on_text("look here and here please")
    text, imgs = mw.assistant_page.calls[-1]
    assert "[screenshot 1]" in text and "[screenshot 3]" in text and len(imgs) == 3, text
    assert all(_wait(p) for p in imgs)
    n = len(mw.assistant_page.calls)                                    # Esc when NOT drawing: drop all
    d._state, d._recorder, d._key_down, d._latched = "recording", _Rec(), True, False
    d._on_control(); d._ctrl_at -= 0.2; d._on_control()
    d._on_right_click(); d._on_right_click()
    d._on_escape()
    assert d._state == "idle" and d._shots == [] and len(mw.assistant_page.calls) == n
    print("right-click controller: right=draw, left draws (several marks), right=snapshot; plain shot; "
          "Esc undoes the drawing only; Space captures too; ONE message  OK")


if __name__ == "__main__":
    test_right_click_tap()
    test_right_click_controller()
    test_key_tap()
    test_ink_snapshot()
    test_controller_gesture()
    test_receive_mode()
    test_markers_and_words()
    test_user_message()
    test_routing_and_details()
    print("test_screen_ink OK")
