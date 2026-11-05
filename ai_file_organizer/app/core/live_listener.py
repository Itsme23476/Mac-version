"""Filect Assistant — hands-free listening (phase 1).

Start (in the Assistant tab) → Filect listens. Whatever you say, once you've stopped talking for "send
after" seconds, is transcribed and handed to the Assistant exactly like an fn + ⌃ request (router →
Claude / Codex → the notch card). What keeps it from picking up the wrong things:
  1. Apple's voice processing in the mic child (mic_helper "go-vp"): background noise and whatever the
     Mac itself plays are removed. It filters the mic for EVERY app while it runs (measured), so the
     listener lets go of the mic the moment anything else records — a Filect hotkey instantly (via
     mic_helper's foreground hook), another app (Zoom, FaceTime, a screen recorder) within ~1 s (via
     CoreAudio's per-process "is capturing input" flag) — and comes back after.
  2. Speech detection (webrtcvad) + a loudness gate: it only starts on ~0.2 s of clear speech that is
     loud enough — you near the mic, not someone across the room.
  3. The router's IGNORE (AssistantPage.run_voice_task, source="live"): speech that isn't a request is
     dropped.
Only speech is sent for transcription (never the silence), so only that counts toward the daily limit.
"""
from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import struct
import threading
import time
from collections import deque
from statistics import median

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger("ai_file_organizer")

RATE = 16000
FRAME = 480                 # 30 ms of 16 kHz audio — webrtcvad's frame
FRAME_SEC = FRAME / RATE
BYTES_PER_SEC = RATE * 2

# sensitivity → (webrtcvad aggressiveness, loudness floor (RMS 0..1), share of YOUR usual loudness a
# sound must reach to count). ponytail: hand-tuned heuristics; if real rooms trip them, swap in a speech
# model (Silero VAD) + per-user calibration.
SENSITIVITY = {
    "low":    (3, 0.006, 0.40),     # ignores more: only clear, close speech
    "medium": (2, 0.003, 0.25),
    "high":   (1, 0.0015, 0.12),    # picks up softer / farther speech
}
START_WINDOW, START_NEEDED = 10, 7     # ≥ 7 of the last 10 frames clear speech (~0.2 s) = you started
RESUME_WINDOW, RESUME_NEEDED = 6, 4    # after a pause, ~0.12 s of clear speech = you went on
GAP_SEC = 0.5               # this much non-speech = a pause: audio stops being sent, the countdown runs
PREROLL_SEC = 0.4           # audio kept from just before speech starts (no clipped first word)
MIN_SPEECH_SEC = 0.6        # less speech than this in total = a cough / "uh" → dropped (never even sent)
MIN_ANSWER_SEC = 0.25       # …unless the agent just asked you something: a short "yes" / "no" counts
# The room's own noise level (measured live): speech must stand clearly above it. Without Apple's filter
# a real room measured ~0.012 RMS and webrtcvad called 80% of it "speech" — so a request never ended.
NOISE_SEC = 5.0             # how much quiet audio the noise estimate looks at
NOISE_WARMUP_SEC = 1.0      # nothing starts until it has heard the room this long
NOISE_START = 4.0           # to start: ≥ 4× the noise floor (+12 dB)
NOISE_CONT = 3.0            # to count as still talking: ≥ 3× (+10 dB)
QUIET_FRAC = 0.3            # "you paused" = under 30% speech in the last GAP_SEC (noise has stray hits)
MIN_WORDS = 2               # a one-word transcript ("okay", "yeah") isn't a request
MAX_UTTERANCE_SEC = 90      # runaway guard (a TV left on): it ends here
IGNORED_MIC_USERS = {"corespeechd", "heard"}   # macOS's always-on Siri / Sound Recognition listeners
_FRIENDLY = {"avconferenced": "FaceTime", "callservicesd": "a phone call"}


def _rms(frame: bytes) -> float:
    import numpy as np
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    return float(math.sqrt(float(np.mean(x * x)))) if len(x) else 0.0


class UtteranceDetector:
    """Pure (no Qt, no mic): 16 kHz int16 audio in → events out. Time is counted in frames, so it's
    exact and testable. Events, in order:
      ("start", preroll_bytes)  you started talking — send this audio (incl. just before you started)
      ("audio", bytes)          more of your speech — send it
      ("pause",)                you paused: stop sending; the "send after" countdown runs
      ("resume",)               you went on before it ran out (the ("audio") after it continues)
      ("end", stats)            you finished → transcribe what was sent; stats: speech_sec, level, secs
      ("drop", stats)           too short to be a request (a cough, "uh") → throw it away
    """

    def __init__(self, send_after: float = 2.0, sensitivity: str = "medium", vad=None):
        self.send_after = float(send_after)
        self._vad_override = vad
        self.set_sensitivity(sensitivity)
        self._refs = deque(maxlen=5)
        self.ref = None                 # your usual speech loudness, learned from real requests
        self.min_speech = MIN_SPEECH_SEC  # the listener lowers it while an answer is expected
        self.reset_noise()
        self.reset()

    def reset_noise(self) -> None:
        """A (re)opened mic — maybe a different one (filter on/off): learn the room again."""
        self._noise = deque(maxlen=int(round(NOISE_SEC / FRAME_SEC)))

    def noise_floor(self) -> float:
        """The room's quiet level: the 20th percentile of recent non-speech-time frames."""
        if len(self._noise) < 10:
            return 0.0
        import numpy as np
        return float(np.percentile(np.fromiter(self._noise, dtype=np.float32), 20))

    def _warm(self) -> bool:
        return len(self._noise) * FRAME_SEC >= NOISE_WARMUP_SEC

    def set_sensitivity(self, sensitivity: str) -> None:
        mode, self.floor, self.ratio = SENSITIVITY.get(sensitivity, SENSITIVITY["medium"])
        self.sensitivity = sensitivity if sensitivity in SENSITIVITY else "medium"
        if self._vad_override is not None:
            self.vad = self._vad_override
            return
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(mode)
        except Exception as e:          # no webrtcvad → loudness alone (still gated by IGNORE)
            logger.warning("[live] webrtcvad unavailable (%s) — loudness-only speech detection", e)
            self.vad = None

    def learn(self, level: float) -> None:
        """A request that turned out real: remember how loud YOU are (the gate is relative to it)."""
        if level and level > 0:
            self._refs.append(float(level))
            self.ref = float(median(self._refs))

    def threshold(self) -> float:
        return max(self.floor, self.ratio * self.ref) if self.ref else self.floor

    def suppress(self) -> None:
        """Esc: drop what you're saying — the REST of that sentence too: nothing starts until you've
        stopped talking (GAP_SEC of non-speech)."""
        keep = (self.ref, self._refs, self._t)
        self.reset()
        self.ref, self._refs, self._t = keep
        self.state = "suppressed"
        self._quiet = 0

    def reset(self) -> None:
        self.state = "idle"             # idle | speaking | trailing | suppressed
        self._rem = b""
        self._pre = deque(maxlen=int(round(PREROLL_SEC / FRAME_SEC)))
        self._win = deque(maxlen=START_WINDOW)
        self._recent = deque(maxlen=int(round(GAP_SEC / FRAME_SEC)))
        self._t = 0                     # frames seen (the clock)
        self._started = self._last_speech = 0
        self._speech_frames = 0
        self._levels = []
        self.last_level = 0.0

    def speech_sec(self) -> float:
        """How much clear speech the request being heard has so far."""
        return self._speech_frames * FRAME_SEC if self.state in ("speaking", "trailing") else 0.0

    def countdown_left(self) -> float | None:
        if self.state != "trailing":
            return None
        return max(0.0, self.send_after - (self._t - self._last_speech) * FRAME_SEC)

    def _frame(self, f: bytes):
        rms = _rms(f)
        if rms < self.floor * 0.5:
            return rms, False
        if self.vad is None:
            return rms, rms >= self.floor
        try:
            return rms, bool(self.vad.is_speech(f, RATE))
        except Exception:
            return rms, rms >= self.floor

    def _stats(self) -> dict:
        return {"speech_sec": round(self._speech_frames * FRAME_SEC, 2),
                "level": round(median(self._levels), 5) if self._levels else 0.0,
                "secs": round((self._last_speech - self._started) * FRAME_SEC, 2)}

    def _finish(self, ev: list, forced: bool = False) -> None:
        st = self._stats()
        st["forced"] = forced
        ev.append(("end" if st["speech_sec"] >= self.min_speech else "drop", st))
        keep_ref, keep_t = (self.ref, self._refs), self._t
        self.reset()
        self.ref, self._refs = keep_ref
        self._t = keep_t

    def feed(self, pcm: bytes) -> list:
        ev, out = [], []

        def flush():
            if out:
                ev.append(("audio", b"".join(out)))
                out.clear()

        data = self._rem + pcm
        n = (len(data) // (FRAME * 2)) * FRAME * 2
        self._rem = data[n:]
        for i in range(0, n, FRAME * 2):
            f = data[i:i + FRAME * 2]
            self._t += 1
            rms, voiced = self._frame(f)
            self.last_level = rms
            if self.state != "speaking":            # learn the room only when you're not talking
                self._noise.append(rms)
            nf = self.noise_floor()
            start_thr = max(self.threshold(), NOISE_START * nf)
            cont_thr = max(self.threshold() * 0.5, NOISE_CONT * nf)
            if self.state == "suppressed":
                self._quiet = 0 if (voiced and rms >= cont_thr) else self._quiet + 1
                if self._quiet * FRAME_SEC >= GAP_SEC:
                    self.state = "idle"
                continue
            if self.state == "idle":
                self._pre.append(f)
                self._win.append(voiced and rms >= start_thr and self._warm())
                if sum(self._win) >= START_NEEDED:
                    self.state = "speaking"
                    self._started = self._last_speech = self._t
                    self._speech_frames = sum(self._win)
                    self._levels = [rms]
                    self._recent.clear()
                    self._recent.extend([True] * len(self._win))
                    ev.append(("start", b"".join(self._pre)))
                    self._pre.clear()
                    self._win = deque(maxlen=RESUME_WINDOW)
            elif self.state == "speaking":
                out.append(f)
                talking = voiced and rms >= cont_thr
                self._recent.append(talking)
                if talking:
                    self._last_speech = self._t
                    self._speech_frames += 1
                    self._levels.append(rms)
                full = len(self._recent) == self._recent.maxlen
                if ((self._t - self._last_speech) * FRAME_SEC >= GAP_SEC
                        or (full and sum(self._recent) < QUIET_FRAC * self._recent.maxlen)):
                    flush()
                    self.state = "trailing"
                    self._pre.clear()
                    self._win.clear()
                    ev.append(("pause",))
                if self.state == "speaking" and (self._t - self._started) * FRAME_SEC >= MAX_UTTERANCE_SEC:
                    flush()
                    self._finish(ev, forced=True)
            elif self.state == "trailing":
                self._pre.append(f)
                self._win.append(voiced and rms >= start_thr)
                if sum(self._win) >= RESUME_NEEDED:
                    self.state = "speaking"
                    self._last_speech = self._t
                    self._recent.clear()
                    self._recent.extend([True] * RESUME_NEEDED)
                    ev.append(("resume",))
                    out.append(b"".join(self._pre))   # the start of what you said next
                    self._pre.clear()
                    self._win.clear()
                elif (self._t - self._last_speech) * FRAME_SEC >= self.send_after:
                    self._finish(ev)
        flush()
        return ev


# ---- who else is using the mic (CoreAudio process objects, macOS 14.2+) ------------------------------
class _Addr(ctypes.Structure):
    _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


_CA = None
_PROC = None


def _coreaudio():
    global _CA, _PROC
    if _CA is None:
        _CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        for fn in (_CA.AudioObjectGetPropertyDataSize, _CA.AudioObjectGetPropertyData):
            fn.restype = ctypes.c_int32
        _CA.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        _CA.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        _PROC = ctypes.CDLL("/usr/lib/libproc.dylib")
        _PROC.proc_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        _PROC.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    return _CA


def _proc_label(pid: int):
    """(executable name, the app it belongs to) — e.g. ("Google Chrome Helper (Renderer)", "Google Chrome"):
    a helper inside an app bundle is named after the app; FaceTime's call service after FaceTime."""
    _coreaudio()
    buf = ctypes.create_string_buffer(4096)
    path = buf.value.decode(errors="replace") if _PROC.proc_pidpath(pid, buf, 4096) > 0 else ""
    base = os.path.basename(path)
    if not base:
        nb = ctypes.create_string_buffer(256)
        _PROC.proc_name(pid, nb, 256)
        base = nb.value.decode(errors="replace") or f"pid {pid}"
    app = next((part[:-4] for part in path.split("/") if part.endswith(".app")), "")
    return base, app or _FRIENDLY.get(base, base)


_OWN = None


def own_label() -> str:
    """The name the mic check gives Filect's own processes (its hotkeys' mic children)."""
    global _OWN
    if _OWN is None:
        try:
            _OWN = _proc_label(os.getpid())[1]
        except Exception:
            _OWN = ""
    return _OWN


def _u32(obj: int, sel: str):
    ca = _coreaudio()
    a = _Addr(_fourcc(sel), _fourcc("glob"), 0)
    v, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(v)):
        return None
    return v.value


def mic_users(exclude_pids=()) -> list:
    """Names of the OTHER processes capturing audio input right now ([] if none, or if this macOS
    can't tell — before 14.2). macOS's own always-on listeners (Siri, Sound Recognition) don't count."""
    try:
        ca = _coreaudio()
        a = _Addr(_fourcc("prs#"), _fourcc("glob"), 0)
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size)):
            return []
        n = size.value // 4
        objs = (ctypes.c_uint32 * n)()
        if ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), objs):
            return []
        out = []
        for o in objs:
            if not _u32(o, "piri"):
                continue
            pid = _u32(o, "ppid")
            if not pid or pid in exclude_pids or pid == os.getpid():
                continue
            base, app = _proc_label(pid)
            if base not in IGNORED_MIC_USERS and app not in out:
                out.append(app)
        return out
    except Exception:
        return []


def screen_locked() -> bool:
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:
        return False


class _Monitor(threading.Thread):
    """Background poll (1 s): other apps using the mic, and the screen lock. Cheap (~0.4 ms CPU)."""

    def __init__(self):
        super().__init__(daemon=True, name="filect-live-monitor")
        self.exclude = set()
        self.others, self.locked = [], False
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self.others = mic_users(set(self.exclude))
            self.locked = screen_locked()
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()


# ---- usage (local estimate of what counts toward the daily voice limit) -----------------------------
def _usage_path():
    from app.core.settings import settings
    return settings.get_app_data_dir() / "assistant_usage.json"


def load_usage_today() -> float:
    try:
        d = json.loads(_usage_path().read_text())
        return float(d.get("seconds", 0.0)) if d.get("day") == time.strftime("%Y-%m-%d") else 0.0
    except Exception:
        return 0.0


def save_usage_today(seconds: float) -> None:
    try:
        p = _usage_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "seconds": round(seconds, 1)}))
        os.replace(tmp, p)
    except Exception:
        pass


class LiveListener(QObject):
    """Owns the always-on mic while Filect Assistant is on: detects each thing you say, transcribes it
    (streaming, only your speech), and emits `heard(id, text)` for the page to run. Lets go of the mic
    whenever something else records, the screen is locked, and resumes after."""

    state_changed = Signal(str, str)      # (state, detail): off|starting|listening|hearing|countdown|paused|error
    level = Signal(float)                 # mic level 0..1, for the meter
    countdown = Signal(float)             # seconds left before what you said is sent (while paused talking)
    utterance_started = Signal(int)
    utterance_ended = Signal(int)         # you stopped talking → it's being transcribed
    heard = Signal(int, str)              # (id, text): run it
    dropped = Signal(int, str, str)       # (id, text, why): not run
    usage = Signal(float)                 # seconds of your voice sent today (≈ what counts toward the limit)
    esc_late = Signal()                   # Esc with nothing being heard/transcribed: the page may cancel a
                                          #   request still being checked by the router
    _chunk_sig = Signal(int, object)
    _end_sig = Signal(int, int)
    _esc_sig = Signal()

    def __init__(self, parent=None, open_mic=None, make_transcriber=None, monitor_factory=None,
                 detector=None):
        super().__init__(parent)
        from app.core import mic_helper
        self._mic = mic_helper
        self._open_mic = open_mic or mic_helper.open_mic
        self._make_tx = make_transcriber or self._default_transcriber
        self._monitor_factory = monitor_factory or _Monitor
        self._det = detector
        self.on = False
        self.state, self._detail = "off", ""
        self._paused = None             # pause reason, or None
        self._clear_since = 0.0
        self._yield_at = 0.0            # when the foreground hook last took the mic from us (any thread)
        self._gen = 0                   # mic generation: blocks/ends from an older mic are ignored
        self._stop_fn = None
        self._mic_vp = False
        self._vp_broken = False
        self._fails = deque(maxlen=6)
        self._uid = 0
        self._cur = None                # (uid, transcriber) being fed right now
        self._pending = {}              # uid → (transcriber, stats) waiting for text
        self._cancelled = {}            # uid → why it was dropped (its text, if it still comes, is ignored)
        self._levels = {}               # uid → speech level (for learn())
        self._used = load_usage_today()
        self._used_day = time.strftime("%Y-%m-%d")
        self._monitor = None
        self._esc_monitors = []
        self._held = []                 # audio of the request being heard, kept on the Mac until it's clearly speech
        self._answer_probe = None       # () -> bool: is the agent waiting for your answer? (set by the page)
        self._chunk_sig.connect(self._on_chunk)
        self._end_sig.connect(self._on_mic_end)
        self._esc_sig.connect(self._on_esc)
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._tick)

    # ---- public ----------------------------------------------------------------------------------
    def start(self) -> None:
        if self.on:
            return
        from app.core.settings import settings
        if self._det is None:
            self._det = UtteranceDetector(settings.live_send_after_sec, settings.live_sensitivity)
        else:
            self._det.send_after = settings.live_send_after_sec
            self._det.set_sensitivity(settings.live_sensitivity)
            self._det.reset()
        self.on = True
        self._paused, self._yield_at, self._vp_broken = None, 0.0, False
        self._fails.clear()
        self._mic.add_foreground_hook(self._fg_hook)
        self._monitor = self._monitor_factory()
        self._monitor.start()
        self._install_esc()
        self._open()
        self._poll.start()
        logger.info("[live] Filect Assistant started (send after %.1fs, sensitivity %s, noise filter %s)",
                    settings.live_send_after_sec, settings.live_sensitivity, settings.live_noise_filter)

    def stop(self) -> None:
        if not self.on:
            return
        self.on = False
        self._poll.stop()
        self._mic.remove_foreground_hook(self._fg_hook)
        self._remove_esc()
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        self._abandon("stopped")
        for uid in list(self._pending):     # finished talking but not transcribed yet: Stop = don't run it
            self._cancelled[uid] = "stopped"
            self._pending.pop(uid, None)
            self.dropped.emit(uid, "", "stopped")
        self._close()
        save_usage_today(self._used)
        self._set_state("off", "")
        logger.info("[live] Filect Assistant stopped")

    def apply_settings(self) -> None:
        """The tab's settings changed while it runs: the next thing you say uses them."""
        from app.core.settings import settings
        if self._det is not None:
            self._det.send_after = settings.live_send_after_sec
            self._det.set_sensitivity(settings.live_sensitivity)
        if self.on and not self._paused and self._mic_vp != (settings.live_noise_filter and not self._vp_broken):
            self._abandon("restarting the mic")
            self._close()
            self._open()

    def set_answer_probe(self, fn) -> None:
        """fn() → True while the agent you're talking to is waiting for your answer (it just asked you
        something, or wants an approval). Then a short "yes" / "no" counts instead of being dropped."""
        self._answer_probe = fn

    def _answer_expected(self) -> bool:
        try:
            return bool(self._answer_probe and self._answer_probe())
        except Exception:
            return False

    def learn(self, uid: int) -> None:
        """That one became a task (it was really you): tune the loudness gate to your voice."""
        lvl = self._levels.pop(uid, None)
        if lvl and self._det is not None:
            self._det.learn(lvl)

    def cancel_current(self) -> None:
        """Esc: throw away what you're saying right now (before it's sent)."""
        if self._cur is None or self._det is None:
            return
        logger.info("[live] cancelled with Esc")
        self._abandon("cancelled")
        self._det.suppress()
        self._set_state("listening", "")

    def _on_esc(self) -> None:
        """Esc — cancels the request at whatever stage it's in, until it actually starts: while you talk
        or pause, while it's being transcribed (here), or while the router checks it (the page:
        esc_late). A late Esc used to do nothing and the request ran."""
        logger.info("[live] Esc (state=%s, hearing=%s, transcribing=%s)", self.state, self._cur is not None,
                    sorted(self._pending))
        if self._cur is not None:
            self.cancel_current()
        elif self._pending:
            uid = max(self._pending)
            self._pending.pop(uid, None)
            self._levels.pop(uid, None)
            self._cancelled[uid] = "cancelled"
            logger.info("[live] utterance %d cancelled with Esc while transcribing", uid)
            self.dropped.emit(uid, "", "cancelled")
        else:
            self.esc_late.emit()

    def used_today(self) -> float:
        return self._used

    # ---- mic ---------------------------------------------------------------------------------------
    def _open(self) -> None:
        from app.core.settings import settings
        vp = bool(settings.live_noise_filter) and not self._vp_broken
        self._gen += 1
        gen = self._gen
        self._mic_vp = vp
        if self._det is not None:
            self._det.reset_noise()         # a new mic (maybe filtered differently): learn the room again
        self._set_state("starting", "Starting the microphone…")
        try:
            self._stop_fn = self._open_mic(lambda b, g=gen: self._chunk_sig.emit(g, b), voice_processing=vp,
                                           background=True,
                                           on_end=lambda rc, g=gen: self._end_sig.emit(g, -1 if rc is None else rc))
        except Exception as e:
            logger.error("[live] mic start failed: %s", e)
            self._stop_fn = None
            self._set_state("error", "Couldn't open the microphone.")
            return
        if self._monitor is not None and getattr(self._stop_fn, "proc", None) is not None:
            self._monitor.exclude = {self._stop_fn.proc.pid}

    def _close(self) -> None:
        self._gen += 1                      # anything still arriving from this mic is ignored
        stop, self._stop_fn = self._stop_fn, None
        if stop is None:
            return
        try:
            stop.proc.kill()                # instant: voice processing must be gone before others record
        except Exception:
            pass
        threading.Thread(target=stop, daemon=True, name="filect-live-mic-stop").start()

    YIELD_SEC = 2.0     # the hook's "step aside" counts this long; by then busy() has taken over

    def _yielding(self) -> bool:
        return time.monotonic() - self._yield_at < self.YIELD_SEC

    def _fg_hook(self) -> None:
        """A Filect hotkey/mic button is about to record (any thread): let go of the mic NOW.
        REGRESSION (his test, 01:10:25): a 2nd shortcut while already paused for the 1st (before it had
        resumed) set a permanent "step aside" flag that only a held mic could clear → paused forever.
        Now it's a timestamp that expires by itself, and only taken when we actually hold the mic."""
        stop = self._stop_fn
        if stop is None:                     # not holding the mic (paused / reopening): nothing to give up
            return
        self._yield_at = time.monotonic()
        try:
            stop.proc.kill()
        except Exception:
            pass

    def _on_mic_end(self, gen: int, rc: int) -> None:
        if gen != self._gen or not self.on or self._paused:
            return
        if self._yielding():
            self._pause("hotkey", "Paused while you use a Filect shortcut")
            return
        if rc == self._mic.VP_UNAVAILABLE and self._mic_vp:
            logger.warning("[live] voice processing unavailable — listening without the noise filter")
            self._vp_broken = True
            self._open()
            return
        self._fails.append(time.monotonic())
        if len(self._fails) >= 5 and self._fails[-1] - self._fails[0] < 60:
            logger.error("[live] the microphone keeps stopping (rc=%s) — giving up", rc)
            self._abandon("microphone stopped")
            self._close()
            self._set_state("error", "The microphone keeps stopping. Click Start to try again.")
            self.on = False
            self._poll.stop()
            return
        logger.warning("[live] mic ended (rc=%s) — reopening", rc)
        self._abandon("microphone restarted")
        self._close()
        QTimer.singleShot(800, lambda: (self.on and not self._paused and self._stop_fn is None) and self._open())

    # ---- pause / resume ------------------------------------------------------------------------------
    def _pause_reason(self):
        if self._yielding() or self._mic.busy():
            return ("hotkey", "Paused while you use a Filect shortcut")
        m = self._monitor
        if m is not None and m.locked:
            return ("locked", "Paused — your Mac is locked")
        if m is not None and m.others:
            others = [n for n in m.others if n != own_label()]
            if not others:                  # Filect's own mic (a shortcut's capture winding down)
                return ("hotkey", "Paused while you use a Filect shortcut")
            return ("other_app", f"Paused while {others[0]} uses the mic")
        return None

    def _pause(self, reason: str, detail: str = "") -> None:
        if self._paused == reason:
            return
        self._paused = reason
        self._abandon("paused")
        if self._det is not None:
            self._det.reset()
        self._close()
        r = self._pause_reason()
        self._set_state("paused", detail or (r[1] if r else "Paused"))
        logger.info("[live] paused (%s)", reason)

    def _tick(self) -> None:
        if not self.on:
            return
        r = self._pause_reason()
        now = time.monotonic()
        if r is not None:
            self._clear_since = 0.0
            if self._paused != r[0]:
                self._pause(r[0], r[1])
            elif self.state == "paused":
                self._set_state("paused", r[1])
        elif self._paused:
            if not self._clear_since:
                self._clear_since = now
            elif now - self._clear_since >= 1.0:          # clear for a second → listen again
                logger.info("[live] resuming (was %s)", self._paused)
                self._paused, self._yield_at, self._clear_since = None, 0.0, 0.0
                self._open()
        if self._det is not None and self.state == "countdown":
            left = self._det.countdown_left()
            if left is not None:
                self.countdown.emit(left)
        if time.strftime("%Y-%m-%d") != self._used_day:       # a new day: the limit resets
            self._used_day, self._used = time.strftime("%Y-%m-%d"), 0.0
            self.usage.emit(0.0)

    # ---- audio → utterances ---------------------------------------------------------------------------
    def _on_chunk(self, gen: int, b: bytes) -> None:
        if gen != self._gen or not self.on or self._paused or self._det is None:
            return
        if self.state == "starting":
            self._set_state("listening", "")
        self.level.emit(min(1.0, _rms(b) * 12.0))
        self._det.min_speech = MIN_ANSWER_SEC if self._answer_expected() else MIN_SPEECH_SEC
        for ev in self._det.feed(b):
            kind = ev[0]
            if kind == "start":
                self._begin(ev[1])
            elif kind == "audio":
                self._send(ev[1])
            elif kind == "pause":
                self._set_state("countdown", "")
                left = self._det.countdown_left()
                self.countdown.emit(left if left is not None else 0.0)
            elif kind == "resume":
                self._set_state("hearing", "")
            elif kind == "end":
                self._finish(ev[1])
            elif kind == "drop":
                self._abandon("too short", stats=ev[1])
                self._set_state("listening", "")
        self._maybe_send()

    def _begin(self, preroll: bytes) -> None:
        """You started talking. Nothing is sent yet: the audio is held here until it's clearly speech
        (_maybe_send) — so a cough or a lone "uh" never leaves the Mac or counts toward your limit."""
        if self._cur is not None:           # shouldn't happen — close the previous one cleanly
            self._abandon("superseded")
        self._uid += 1
        self._cur = [self._uid, None]       # [id, transcriber (None until it's clearly speech)]
        self._held = [preroll]
        self._set_state("hearing", "")
        self.utterance_started.emit(self._uid)

    def _maybe_send(self) -> None:
        """Once what you're saying is clearly speech (≥ min_speech), start transcribing: everything held
        so far goes up at once (streaming catches up instantly), the rest streams as you talk."""
        cur = self._cur
        if cur is None or cur[1] is not None or self._det is None or self._det.speech_sec() < self._det.min_speech:
            return
        uid = cur[0]
        try:
            tr = self._make_tx()
            tr._live_uid = uid
            tr.finished.connect(self._on_tx_text)
            tr.error.connect(self._on_tx_error)
            tr.start()
        except Exception as e:
            logger.error("[live] couldn't start transcription: %s", e)
            self._cur, self._held = None, []
            self.dropped.emit(uid, "", "Couldn't start transcription")
            return
        cur[1] = tr
        held, self._held = self._held, []
        for b in held:
            self._send(b)

    def _send(self, data: bytes) -> None:
        if self._cur is None or not data:
            return
        if self._cur[1] is None:            # not clearly speech yet: keep it here
            self._held.append(data)
            return
        self._cur[1].feed(data)
        self._used += len(data) / BYTES_PER_SEC
        self.usage.emit(self._used)

    def _finish(self, stats: dict) -> None:
        self._maybe_send()                  # (enough speech arrived in this very block)
        cur, self._cur = self._cur, None
        self._set_state("listening", "")
        if cur is None:
            return
        uid, tr = cur
        if tr is None:                      # (can't happen: "end" means ≥ min_speech) — nothing was sent
            self._held = []
            self.dropped.emit(uid, "", "too short")
            return
        self._pending[uid] = (tr, stats)
        self._levels[uid] = stats.get("level", 0.0)
        tr.stop_recording()                 # → final text (streaming), or the batch fallback
        self.utterance_ended.emit(uid)
        save_usage_today(self._used)
        logger.info("[live] utterance %d ended: %s", uid, stats)

    def _abandon(self, why: str, stats=None) -> None:
        """Drop the utterance being heard right now (its text, if any, is ignored)."""
        cur, self._cur = self._cur, None
        self._held = []
        if cur is None:
            return
        uid, tr = cur
        if tr is not None:                  # it was being sent: stop it, and no batch upload after
            self._cancelled[uid] = why
            try:
                (tr.cancel if hasattr(tr, "cancel") else tr.stop_recording)()
            except Exception:
                pass
        self.dropped.emit(uid, "", why)
        logger.info("[live] utterance %d dropped: %s %s", uid, why, stats or "")

    def _on_tx_text(self, text: str) -> None:
        tr = self.sender()
        uid = getattr(tr, "_live_uid", None)
        if uid is None:
            return
        self._pending.pop(uid, None)
        if uid in self._cancelled:
            why = self._cancelled.pop(uid)
            if why == "cancelled" and (text or "").strip():
                self.dropped.emit(uid, text.strip(), "cancelled")   # show WHAT was cancelled (still not run)
            return
        text = (text or "").strip()
        min_words = 1 if self._answer_expected() else MIN_WORDS     # "yes" is an answer when one is expected
        if len(text.split()) < min_words:
            self._levels.pop(uid, None)
            self.dropped.emit(uid, text, "too short to be a request" if text else "no words recognized")
            return
        logger.info("[live] heard %d: %r", uid, text)
        self.heard.emit(uid, text)

    def _on_tx_error(self, msg: str) -> None:
        tr = self.sender()
        uid = getattr(tr, "_live_uid", None)
        if uid is None:
            return
        self._pending.pop(uid, None)
        self._levels.pop(uid, None)
        if uid in self._cancelled:
            self._cancelled.pop(uid, None)
            return
        self.dropped.emit(uid, "", f"Couldn't transcribe: {msg}")
        if "limit" in (msg or "").lower():          # the daily voice limit → stop listening
            self.stop()
            self._set_state("error", "Daily voice limit reached — Filect Assistant stopped.")

    @staticmethod
    def _default_transcriber():
        from app.core.settings import settings
        from app.core.transcription import StreamingTranscriber
        return StreamingTranscriber(terms=settings.dictation_custom_terms,
                                    language=(settings.dictation_language or None), external=True)

    # ---- Esc (observe only — never swallowed, so Esc still works in your apps) -------------------------
    def _install_esc(self) -> None:
        try:
            import AppKit
            mask = AppKit.NSEventMaskKeyDown

            def _global(ev):
                if ev.keyCode() == 53:
                    self._esc_sig.emit()

            def _local(ev):
                _global(ev)
                return ev

            g = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, _global)
            loc = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, _local)
            self._esc_monitors = [m for m in (g, loc) if m is not None]
        except Exception as e:
            logger.info("[live] Esc monitor unavailable: %s", e)
            self._esc_monitors = []

    def _remove_esc(self) -> None:
        try:
            import AppKit
            for m in self._esc_monitors:
                AppKit.NSEvent.removeMonitor_(m)
        except Exception:
            pass
        self._esc_monitors = []

    def _set_state(self, state: str, detail: str) -> None:
        if state == self.state and detail == getattr(self, "_detail", ""):
            return
        self.state, self._detail = state, detail
        self.state_changed.emit(state, detail)
