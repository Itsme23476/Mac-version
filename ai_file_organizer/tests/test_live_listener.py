"""Filect Assistant (hands-free) — the listener. Real speech made with macOS `say` (no mic), a fake mic
and a fake transcriber, so every rule is exercised exactly:
  · one sentence → one request; a short pause (< "send after") stays ONE request; a long one → two
  · coughs / one-word noises are dropped; a quiet far-away voice doesn't start anything (once it knows you)
  · only your speech is sent (never the silence before/after) — that's what counts toward the limit
  · it lets go of the mic for a Filect hotkey (instantly), another app using the mic, a locked screen,
    and comes back after; no voice processing → plain mic; a mic that keeps dying → stops with an error
  · Esc cancels what you're saying; Stop drops what's still being transcribed; a one-word result and the
    daily-limit error are handled
Your settings.json is never written; the usage file goes to a temp dir.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_live_listener.py"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from app.core.settings import settings  # noqa: E402
settings._save_config = lambda: None            # NEVER write the user's settings.json
from app.core import live_listener as ll  # noqa: E402
from app.core import mic_helper  # noqa: E402

_TMP = Path(tempfile.mkdtemp())
ll._usage_path = lambda: _TMP / "assistant_usage.json"
settings.live_send_after_sec, settings.live_sensitivity, settings.live_noise_filter = 2.0, "medium", True
RATE = 16000
_CACHE = {}


def speech(text: str, gain: float = 0.35) -> np.ndarray:
    """`text` spoken by macOS, 16 kHz int16. gain 0.35 ≈ a person close to the mic (RMS ~0.04)."""
    if text not in _CACHE:
        d = tempfile.mkdtemp()
        subprocess.run(["say", "-o", f"{d}/s.aiff", text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"{d}/s.aiff", f"{d}/s.wav"],
                       check=True)
        import wave
        w = wave.open(f"{d}/s.wav")
        _CACHE[text] = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    return np.clip(_CACHE[text] * gain, -32768, 32767).astype(np.int16)


def silence(secs: float, noise: float = 0.0004) -> np.ndarray:
    """Room silence after Apple's noise filter (measured ~0.0002–0.0005 RMS)."""
    rng = np.random.default_rng(1)
    return (rng.standard_normal(int(secs * RATE)) * noise * 32768).astype(np.int16)


def run_detector(audio: np.ndarray, send_after: float = 2.0, det=None, block: int = 1600):
    det = det or ll.UtteranceDetector(send_after, "medium")
    events, sent = [], 0
    b = audio.tobytes()
    for i in range(0, len(b), block * 2):
        for ev in det.feed(b[i:i + block * 2]):
            events.append(ev)
            if ev[0] in ("start", "audio"):
                sent += len(ev[1])
    return events, sent / (RATE * 2), det


def kinds(events):
    return [e[0] for e in events if e[0] != "audio"]


def _pump(cond, secs):
    end = time.time() + secs
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


# ---- the detector ---------------------------------------------------------------------------------
def test_one_sentence_is_one_request_and_only_speech_is_sent():
    s = speech("Create a new folder on my desktop called invoices.")
    audio = np.concatenate([silence(1.5), s, silence(4.0)])
    ev, sent, _ = run_detector(audio)
    assert kinds(ev) == ["start", "pause", "end"], kinds(ev)
    st = ev[-1][1]
    assert st["speech_sec"] > 1.5 and st["level"] > 0.01, st
    spoken = len(s) / RATE
    assert spoken - 0.3 < sent < spoken + 1.0, (sent, spoken)       # speech + 0.4 s lead-in + the 0.5 s gap
    assert sent < len(audio) / RATE - 3.5, "the silence before/after was never sent"
    print(f"one sentence → one request; sent {sent:.2f}s of {len(audio)/RATE:.1f}s (speech {spoken:.2f}s)  OK")


def test_send_after_timing():
    s = speech("Summarize my last three emails please.")
    for send_after in (1.0, 2.0, 4.0):
        audio = np.concatenate([silence(1.0), s, silence(send_after + 2.0)])
        det = ll.UtteranceDetector(send_after, "medium")
        b, t_end, t = audio.tobytes(), None, 0
        for i in range(0, len(b), 3200):
            evs = det.feed(b[i:i + 3200])
            t += 0.1
            if any(e[0] == "end" for e in evs) and t_end is None:
                t_end = t
        speech_end = 1.0 + len(s) / RATE
        assert t_end is not None and send_after - 0.6 < t_end - speech_end < send_after + 0.5, (send_after, t_end, speech_end)
    print("ends 'send after' seconds after you stop talking (1 s / 2 s / 4 s)  OK")


def test_short_pause_stays_one_request_long_pause_makes_two():
    a, b = speech("Open my project folder."), speech("And then rename the latest file to final.")
    one = np.concatenate([silence(1.0), a, silence(1.0), b, silence(3.5)])
    ev, _, _ = run_detector(one, send_after=2.0)
    assert kinds(ev) == ["start", "pause", "resume", "pause", "end"], kinds(ev)
    two = np.concatenate([silence(1.0), a, silence(3.5), b, silence(3.5)])
    ev, _, _ = run_detector(two, send_after=2.0)
    assert kinds(ev) == ["start", "pause", "end", "start", "pause", "end"], kinds(ev)
    print("a 1 s pause stays one request; a 3.5 s pause makes two  OK")


def test_noise_and_coughs_dont_count():
    ev, sent, _ = run_detector(silence(6.0))
    assert ev == [] and sent == 0, ev
    rng = np.random.default_rng(3)
    clicks = silence(4.0)
    for at in (0.5, 1.6, 2.9):                                    # door knocks / keyboard thumps
        i = int(at * RATE)
        clicks[i:i + 800] = (rng.standard_normal(800) * 0.3 * 32768).clip(-32768, 32767).astype(np.int16)
    ev, _, _ = run_detector(clicks)
    assert "end" not in kinds(ev), kinds(ev)
    cough = np.concatenate([silence(1.0), speech("Hm.", 0.5), silence(3.0)])
    ev, _, _ = run_detector(cough)
    assert "end" not in kinds(ev), kinds(ev)                      # never started, or dropped as too short
    print("room noise, knocks and a lone 'hm' never become a request  OK")


def babble(secs: float, level: float = 0.012) -> np.ndarray:
    """Speech-LIKE room noise (a TV / people talking far away + hiss) — what fooled the detector live
    with the filter off: webrtcvad called 80% of the real room "speech"."""
    parts = [speech(t, 1.0).astype(np.float32) for t in (
        "The weather tomorrow will be mostly sunny with a few clouds in the afternoon.",
        "And in other news the market closed slightly higher today after a quiet session.",
        "Welcome back to the show, tonight we have a very special guest joining us.")]
    n = int(secs * RATE)
    mix = np.zeros(n, dtype=np.float32)
    for k, p in enumerate(parts):
        rep = np.tile(p, int(np.ceil(n / len(p))) + 1)
        off = int(k * 0.37 * RATE)
        mix += rep[off:off + n]
    mix += np.random.default_rng(5).standard_normal(n).astype(np.float32) * 2000
    mix *= level * 32768 / float(np.sqrt(np.mean(mix ** 2)))
    return mix.clip(-32768, 32767).astype(np.int16)


def test_noisy_room_without_the_filter():
    """REGRESSION (live, filter off): the request never ended in a noisy room. Now the room's own level
    is learned and speech must stand clearly above it."""
    ev, sent, _ = run_detector(babble(12.0))
    assert "end" not in kinds(ev), ("background chatter alone became a request", kinds(ev))
    s = speech("What is seven times eight? Just answer with the number.", 0.6)   # you, close to the mic
    bg = babble(1.5 + len(s) / RATE + 5.0)
    fg = np.concatenate([np.zeros(int(1.5 * RATE), np.int16), s, np.zeros(int(5.0 * RATE), np.int16)])
    mixed = (bg.astype(np.int32) + fg.astype(np.int32)).clip(-32768, 32767).astype(np.int16)
    det = ll.UtteranceDetector(2.0, "medium")
    b, t_end, t = mixed.tobytes(), None, 0
    evs = []
    for i in range(0, len(b), 3200):
        e = det.feed(b[i:i + 3200])
        evs += e
        t += 0.1
        if any(x[0] == "end" for x in e) and t_end is None:
            t_end = t
    speech_end = 1.5 + len(s) / RATE
    assert t_end is not None, ("never ended", kinds(evs))
    assert t_end - speech_end < 2.0 + 1.0, (t_end, speech_end)
    st = [x[1] for x in evs if x[0] == "end"][0]
    print(f"noisy room (chatter at 0.012, floor {det.noise_floor():.4f}): chatter alone → nothing; your request "
          f"→ ended {t_end - speech_end:.1f}s after you stopped (send-after 2 s), speech {st['speech_sec']}s  OK")


def test_far_voice_ignored_once_it_knows_you():
    near = speech("Find the contract I signed last week.", 0.35)
    far = speech("Did you watch the game last night?", 0.035)    # 20 dB quieter: across the room
    det = ll.UtteranceDetector(2.0, "medium")
    ev, _, det = run_detector(np.concatenate([silence(1.0), near, silence(3.0)]), det=det)
    det.learn(ev[-1][1]["level"])                                 # that one was really you
    ev, sent, _ = run_detector(np.concatenate([silence(1.0), far, silence(3.0)]), det=det)
    assert kinds(ev) == [] and sent == 0, kinds(ev)
    ev, _, _ = run_detector(np.concatenate([silence(1.0), near, silence(3.0)]), det=det)
    assert kinds(ev)[-1] == "end"                                 # you still are heard
    print(f"after learning your voice (level {det.ref:.3f}), a voice 20 dB quieter is ignored  OK")


def test_sensitivity_changes_the_floor():
    soft = speech("Please open the downloads folder.", 0.04)
    audio = np.concatenate([silence(1.0), soft, silence(3.0)])
    got = {s: kinds(run_detector(audio, det=ll.UtteranceDetector(2.0, s))[0]) for s in ("low", "medium", "high")}
    assert "end" in got["high"] and "end" not in got["low"], got
    print(f"a soft voice: high sensitivity hears it, low ignores it ({got})  OK")


# ---- the listener (fake mic + fake transcriber) ---------------------------------------------------
class FakeProc:
    def __init__(self, pid):
        self.pid, self.killed = pid, False

    def kill(self):
        self.killed = True


class FakeMic:
    """Records each open_mic call; push(audio) delivers blocks like the reader thread does."""

    def __init__(self):
        self.calls = []

    def __call__(self, on_chunk, voice_processing=False, background=False, on_end=None):
        stop_calls = []

        def stop():
            stop_calls.append(1)
        stop.proc = FakeProc(1000 + len(self.calls))
        stop.calls = stop_calls
        self.calls.append({"vp": voice_processing, "bg": background, "on_chunk": on_chunk, "on_end": on_end,
                           "stop": stop})
        return stop

    def push(self, audio, k=-1):
        cb = self.calls[k]["on_chunk"]
        b = audio.tobytes()
        for i in range(0, len(b), 3200):
            cb(b[i:i + 3200])
        app.processEvents()


class FakeTx(QObject):
    finished = Signal(str)
    error = Signal(str)
    reply = "Create a new folder called invoices"
    made = []

    def __init__(self):
        super().__init__()
        self.fed, self.stopped = 0, False
        FakeTx.made.append(self)

    def start(self):
        pass

    def feed(self, data):
        self.fed += len(data)

    def stop_recording(self):
        self.stopped = True
        r = FakeTx.reply
        if isinstance(r, Exception):
            QTimer.singleShot(10, lambda: self.error.emit(str(r)))
        else:
            QTimer.singleShot(10, lambda: self.finished.emit(r))


class FakeMonitor:
    others, locked, exclude, started = [], False, set(), 0

    def __init__(self):
        FakeMonitor.started += 1

    def start(self):
        pass

    def stop(self):
        pass


def _listener():
    FakeTx.made.clear()
    FakeTx.reply = "Create a new folder called invoices"
    FakeMonitor.others, FakeMonitor.locked = [], False
    mic = FakeMic()
    L = ll.LiveListener(open_mic=mic, make_transcriber=FakeTx, monitor_factory=lambda: FakeMonitor())
    rec = {"states": [], "heard": [], "dropped": [], "ended": []}
    L.state_changed.connect(lambda s, d: rec["states"].append((s, d)))
    L.heard.connect(lambda u, t: rec["heard"].append((u, t)))
    L.dropped.connect(lambda u, t, w: rec["dropped"].append((u, t, w)))
    L.utterance_ended.connect(lambda u: rec["ended"].append(u))
    return L, mic, rec


def test_listener_hears_a_request():
    L, mic, rec = _listener()
    L.start()
    try:
        assert mic.calls[0]["vp"] and mic.calls[0]["bg"], "noise-filtered + background (doesn't mark the mic busy)"
        assert L._monitor is not None and FakeMonitor.exclude == {1000} or L._monitor.exclude == {1000}
        s = speech("Create a new folder on my desktop called invoices.")
        mic.push(np.concatenate([silence(1.0), s, silence(3.0)]))
        assert _pump(lambda: rec["heard"], 2), rec
        assert rec["heard"] == [(1, "Create a new folder called invoices")]
        assert rec["ended"] == [1] and FakeTx.made[0].stopped
        fed = FakeTx.made[0].fed / 32000
        assert fed < len(s) / RATE + 1.0 and abs(L.used_today() - fed) < 0.01, (fed, L.used_today())
        assert ll.load_usage_today() == round(L.used_today(), 1)
        states = [s for s, _ in rec["states"]]
        assert states[:2] == ["starting", "listening"] and "hearing" in states and "countdown" in states
        L.learn(1)
        assert L._det.ref and L._det.ref > 0.01
    finally:
        L.stop()
    assert rec["states"][-1][0] == "off" and mic.calls[0]["stop"].proc.killed
    print("listener: speech → 'heard' with the text; only speech fed + counted; usage saved; learns your level  OK")


def test_one_word_and_errors():
    L, mic, rec = _listener()
    L.start()
    try:
        FakeTx.reply = "Okay."
        mic.push(np.concatenate([silence(0.5), speech("Okay, sounds good to me then."), silence(3.0)]))
        assert _pump(lambda: rec["dropped"], 2) and not rec["heard"]
        assert rec["dropped"][0][2] == "too short to be a request", rec["dropped"]
        FakeTx.reply = RuntimeError("Daily voice limit reached. Try again tomorrow.")
        mic.push(np.concatenate([silence(0.5), speech("Open my calendar for tomorrow please."), silence(3.0)]))
        assert _pump(lambda: rec["states"][-1][0] == "error", 2), rec["states"][-3:]
        assert not L.on and "limit" in rec["states"][-1][1]
    finally:
        L.stop()
    print("a one-word result is dropped; the daily-limit error stops listening with a message  OK")


def test_esc_and_stop_drop_what_isnt_finished():
    L, mic, rec = _listener()
    L.start()
    try:
        s = speech("Delete all the files in my downloads folder.")
        mic.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))       # mid-sentence…
        assert L._cur is not None
        L.cancel_current()                                                # …Esc
        assert rec["dropped"][-1][2] == "cancelled" and L._cur is None
        mic.push(np.concatenate([s[len(s) // 2:], silence(3.0)]))       # you keep talking: the rest of that
        _pump(lambda: False, 0.3)                                        # sentence is NOT a new request
        _pump(lambda: False, 0.3)
        assert not rec["heard"], rec["heard"]
        FakeTx.reply = "Rename the folder to archive"
        mic.push(np.concatenate([silence(1.0), speech("Rename the folder to archive."), silence(2.1)]))
        n = len(rec["dropped"])
        L.stop()                                                          # ended, still transcribing → Stop
        _pump(lambda: False, 0.3)
        assert not rec["heard"], "Stop: nothing runs after you stopped"
        assert any(d[2] == "stopped" for d in rec["dropped"][n:]) or not L._pending
    finally:
        L.stop()
    print("Esc mid-sentence drops it; Stop drops what was still being transcribed  OK")


def test_lets_go_of_the_mic_and_comes_back():
    L, mic, rec = _listener()
    real_busy = mic_helper.busy
    busy = {"v": False}
    mic_helper.busy = lambda: busy["v"]
    L.start()
    try:
        assert _pump(lambda: True, 0.05)
        # 1) a Filect hotkey: the foreground hook kills the voice-processing child BEFORE it records
        first = mic.calls[-1]
        for fn in list(mic_helper._fg_hooks):
            fn()
        assert first["stop"].proc.killed, "killed before the hotkey's capture starts"
        busy["v"] = True
        first["on_end"](-9)                                              # the child died
        assert _pump(lambda: L.state == "paused", 1), L.state
        assert "Filect shortcut" in rec["states"][-1][1]
        _pump(lambda: False, 1.6)
        assert len(mic.calls) == 1, "stays paused while the hotkey records"
        busy["v"] = False
        assert _pump(lambda: len(mic.calls) == 2, 3), "comes back ~1 s after"
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        assert _pump(lambda: L.state == "listening", 1)
        # 2) another app records (Zoom)
        FakeMonitor.others = ["zoom.us"]
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["states"][-1][1] == "Paused while zoom.us uses the mic" and mic.calls[-1]["stop"].proc.killed
        FakeMonitor.others = []
        assert _pump(lambda: len(mic.calls) == 3, 3)
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        FakeMonitor.others = [ll.own_label()]                          # Filect's own capture winding down
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["states"][-1][1] == "Paused while you use a Filect shortcut", rec["states"][-1]
        FakeMonitor.others = []
        assert _pump(lambda: len(mic.calls) == 4, 3)
        # 3) the screen locks
        FakeMonitor.locked = True
        assert _pump(lambda: L.state == "paused", 1.5) and "locked" in rec["states"][-1][1]
        FakeMonitor.locked = False
        assert _pump(lambda: len(mic.calls) == 5, 3)
        # 4) mid-sentence when a hotkey fires → that sentence is dropped, not half-sent
        mic.calls[-1]["on_chunk"](silence(0.1).tobytes())
        s = speech("Move these into the photos folder.")
        mic.push(np.concatenate([silence(0.5), s[: len(s) // 2]]))
        assert L._cur is not None
        busy["v"] = True
        assert _pump(lambda: L.state == "paused", 1.5)
        assert rec["dropped"][-1][2] == "paused" and not rec["heard"]
        busy["v"] = False
    finally:
        mic_helper.busy = real_busy
        L.stop()
    assert L._fg_hook not in mic_helper._fg_hooks, "the hook is removed on Stop"
    print("lets go of the mic for a hotkey (killed first), Zoom, a locked screen — and comes back after  OK")


def test_no_voice_processing_and_a_dying_mic():
    L, mic, rec = _listener()
    L.start()
    try:
        mic.calls[-1]["on_end"](mic_helper.VP_UNAVAILABLE)
        assert _pump(lambda: len(mic.calls) == 2, 1) and mic.calls[-1]["vp"] is False, "plain mic instead"
        for i in range(5):                                               # keeps dying
            mic.calls[-1]["on_end"](-9)
            _pump(lambda n=len(mic.calls): len(mic.calls) > n or L.state == "error", 1.5)
        assert L.state == "error" and not L.on, (L.state, len(mic.calls))
        assert "keeps stopping" in rec["states"][-1][1]
    finally:
        L.stop()
    print("no voice processing → plain mic; a mic that keeps dying → stops with an error  OK")


def test_real_noise_filtered_mic():
    """The REAL mic child in noise-filter mode for 3 s: 16 kHz audio arrives; it doesn't mark the mic
    busy; Stop ends it. (Records the room for 3 s — nothing is sent anywhere.)"""
    got = []
    stop = mic_helper.open_mic(got.append, voice_processing=True, background=True)
    try:
        assert not mic_helper.busy()
        _pump(lambda: len(b"".join(got)) > 32000, 6)
    finally:
        stop()
    n = len(b"".join(got)) // 2
    assert n > 16000, n
    print(f"real noise-filtered mic: {n/RATE:.1f}s of 16 kHz audio, mic not marked busy  OK")


def test_mic_users_runs_fast():
    t0 = time.process_time()
    for _ in range(5):
        users = ll.mic_users()
    cpu = (time.process_time() - t0) / 5 * 1000
    assert isinstance(users, list) and "corespeechd" not in users
    assert cpu < 20, cpu
    print(f"other-app mic check: {users} ({cpu:.1f} ms CPU)  OK")


if __name__ == "__main__":
    test_one_sentence_is_one_request_and_only_speech_is_sent()
    test_send_after_timing()
    test_short_pause_stays_one_request_long_pause_makes_two()
    test_noise_and_coughs_dont_count()
    test_noisy_room_without_the_filter()
    test_far_voice_ignored_once_it_knows_you()
    test_sensitivity_changes_the_floor()
    test_listener_hears_a_request()
    test_one_word_and_errors()
    test_esc_and_stop_drop_what_isnt_finished()
    test_lets_go_of_the_mic_and_comes_back()
    test_no_voice_processing_and_a_dying_mic()
    test_real_noise_filtered_mic()
    test_mic_users_runs_fast()
    print("test_live_listener OK")
