"""
Mic capture in a throwaway child process.

Why: PortAudio on macOS can deadlock inside Pa_StopStream — AudioOutputUnitStop holds the
AudioUnit lock and waits on the HAL IO context, while CoreAudio's IO thread holds that context and
runs PortAudio's IsRunning listener, which wants the AudioUnit lock. Once that happens the
process's HAL device stays locked forever, so every later Pa_OpenStream blocks and voice is dead
until Filect restarts (seen live 2026-10-04, 10-28, 11-02, 11-03 — `sample` showed exactly this).

So the mic lives in a child that is never stopped: it streams raw int16 PCM to stdout and exits
with os._exit() — the stream is torn down by process death, which can't deadlock. A spare child is
kept loaded (sounddevice imported, mic NOT open) so a capture starts instantly.

Hands-free mode (Filect Assistant) asks the child for "go-vp" instead: Apple's voice processing (the
FaceTime pipeline — noise suppression + echo cancellation of what the Mac itself plays) through
AVAudioEngine, resampled to the same 16 kHz int16 mono stream. Torn down the same way (process death).
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK = 1600           # samples per block (100 ms) — same block size the in-process stream used
VP_UNAVAILABLE = 4     # child exit code: voice processing couldn't start (odd device / old macOS)
VP_STALLED = 5         # child exit code: voice-processed audio stopped arriving (device changed)

_spare = None
_spare_lock = threading.Lock()
_busy = 0              # foreground captures running (hotkeys, mic buttons) — hands-free pauses meanwhile
_busy_lock = threading.Lock()
_fg_hooks = []         # called right BEFORE a foreground capture starts (any thread)


def busy() -> bool:
    """Is a foreground capture (dictation, fn + ⌃, voice search/organize, a mic button) running?"""
    return _busy > 0


def add_foreground_hook(fn) -> None:
    """fn() runs right before every foreground capture starts, on the capturing thread. The hands-free
    listener uses it to let go of the mic first: Apple's voice processing filters the mic for EVERY app
    while it runs, so it must be gone before dictation records (measured: then the very first 100 ms of
    dictation are clean)."""
    if fn not in _fg_hooks:
        _fg_hooks.append(fn)


def remove_foreground_hook(fn) -> None:
    try:
        _fg_hooks.remove(fn)
    except ValueError:
        pass


class Resampler:
    """Streaming float32 resampler (any rate → 16 kHz): a windowed-sinc low-pass, then linear
    interpolation on a continuous timeline, so block edges never click. Pure numpy."""

    def __init__(self, rate_in: float, rate_out: float = SAMPLE_RATE, taps: int = 63):
        import numpy as np
        self.ratio = float(rate_in) / float(rate_out)
        self.passthrough = abs(self.ratio - 1.0) < 1e-9
        cutoff = 0.45 / self.ratio                        # just under the output Nyquist, per input sample
        n = np.arange(taps) - (taps - 1) / 2.0
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        self.h = (h / h.sum()).astype(np.float32)
        self.hist = np.zeros(taps - 1, dtype=np.float32)  # filter memory across blocks
        self.prev = np.zeros(0, dtype=np.float32)        # filtered samples kept for interpolation
        self.pos = 0.0                                    # next output time, in input samples (relative to prev[0])

    def process(self, x):
        import numpy as np
        x = np.asarray(x, dtype=np.float32)
        if self.passthrough:
            return x
        y = np.convolve(np.concatenate([self.hist, x]), self.h, mode="valid")   # len(x) samples
        self.hist = np.concatenate([self.hist, x])[-(len(self.h) - 1):]
        buf = np.concatenate([self.prev, y])
        last = len(buf) - 1                               # can interpolate up to t < last
        if self.pos >= last:
            self.prev = buf
            return np.zeros(0, dtype=np.float32)
        t = np.arange(self.pos, last, self.ratio)
        out = np.interp(t, np.arange(len(buf)), buf).astype(np.float32)
        nxt = self.pos + len(t) * self.ratio
        keep = int(nxt)                                   # drop what's fully consumed
        self.prev = buf[keep:]
        self.pos = nxt - keep
        return out


def serve():
    """Child (FILECT_MODE=mic-helper): wait for 'go' (or 'go-vp'), then write mic PCM to stdout until
    'stop'/EOF."""
    import sounddevice as sd
    out = sys.stdout.buffer
    cmd = sys.stdin.readline()
    if cmd.startswith("go-vp"):
        _serve_vp(out)                               # never returns
    if not cmd.startswith("go"):                     # parent gone / spare discarded
        os._exit(0)
    lock, done = threading.Lock(), [False]

    def cb(indata, frames, time_info, status):
        with lock:
            if done[0]:
                return
            try:
                out.write(bytes(indata))
                out.flush()
            except Exception:
                os._exit(0)

    try:
        sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                          blocksize=CHUNK, callback=cb).start()
    except Exception:
        os._exit(3)                                  # no mic → parent sees EOF with no audio
    sys.stdin.readline()                             # "stop", or EOF if the parent died
    with lock:                                       # no half-written block after this
        done[0] = True
    os._exit(0)                                      # never Pa_StopStream: that's the call that deadlocks


def _serve_vp(out):
    """Child, hands-free mode: Apple's voice processing via AVAudioEngine — noise suppression + echo
    cancellation (what the Mac itself plays is removed). Automatic gain is OFF so loudness stays real:
    you, near the mic, are louder than someone across the room — the listener relies on that. Other
    apps' sound is ducked as little as macOS allows. Exits VP_UNAVAILABLE if it can't start, and
    VP_STALLED if audio stops arriving (e.g. the input device changed) — the parent then respawns."""
    try:
        import numpy as np
        import AVFoundation as AV
        eng = AV.AVAudioEngine.alloc().init()
        inp = eng.inputNode()
        ok, _err = inp.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            os._exit(VP_UNAVAILABLE)
        try:
            inp.setVoiceProcessingAGCEnabled_(False)
        except Exception:
            pass
        try:                                          # macOS 14+: duck other apps' audio the least
            inp.setVoiceProcessingOtherAudioDuckingConfiguration_(
                (False, AV.AVAudioVoiceProcessingOtherAudioDuckingLevelMin))
        except Exception:
            pass
        fmt = inp.outputFormatForBus_(0)
        rate = float(fmt.sampleRate())
        if rate <= 0:
            os._exit(VP_UNAVAILABLE)
    except Exception:
        os._exit(VP_UNAVAILABLE)
    rs = Resampler(rate, SAMPLE_RATE)
    lock, done, last = threading.Lock(), [False], [time.monotonic()]

    def tap(buf, _when):
        try:
            n = int(buf.frameLength())
            x = np.frombuffer(buf.floatChannelData()[0].as_buffer(n), dtype=np.float32)   # channel 0
            pcm = (np.clip(rs.process(x), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        except Exception:
            return
        with lock:
            if done[0]:
                return
            last[0] = time.monotonic()
            try:
                out.write(pcm)
                out.flush()
            except Exception:
                os._exit(0)

    try:
        inp.installTapOnBus_bufferSize_format_block_(0, int(rate // 10), fmt, tap)
        eng.prepare()
        ok, _err = eng.startAndReturnError_(None)
        if not ok:
            os._exit(VP_UNAVAILABLE)
    except Exception:
        os._exit(VP_UNAVAILABLE)

    def watchdog():
        while True:
            time.sleep(0.5)
            with lock:
                if done[0]:
                    return
                stalled = time.monotonic() - last[0] > 3.0
            if stalled:
                os._exit(VP_STALLED)

    threading.Thread(target=watchdog, daemon=True).start()
    sys.stdin.readline()                             # "stop", or EOF if the parent died
    with lock:
        done[0] = True
    os._exit(0)                                      # torn down by process death, like the PortAudio child


def _spawn():
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]                       # .app/Contents/MacOS/Filect
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parents[2] / "main.py")]
    return subprocess.Popen(cmd, env={**os.environ, "FILECT_MODE": "mic-helper"},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


def warm():
    """Make sure a loaded helper is waiting, so the next capture starts with no delay."""
    global _spare
    with _spare_lock:
        if _spare is None or _spare.poll() is not None:
            _spare = _spawn()


def open_mic(on_chunk, voice_processing: bool = False, background: bool = False, on_end=None):
    """Start capturing. on_chunk(bytes) runs on a reader thread for every block.
    Returns stop(): ends the capture after every block already captured has been delivered.
    voice_processing: Apple's noise suppression + echo cancellation (the hands-free listener).
    background: don't count as a foreground capture (busy()) — the hands-free listener's own mic.
    on_end(returncode): called from the reader thread when the child's audio ends (it exited/crashed)."""
    global _spare, _busy
    if not background:
        for fn in list(_fg_hooks):
            try:
                fn()
            except Exception:
                pass
    with _spare_lock:
        p, _spare = _spare, None
    if p is None or p.poll() is not None:
        p = _spawn()
    p.stdin.write(b"go-vp\n" if voice_processing else b"go\n")
    p.stdin.flush()
    counted = not background
    if counted:
        with _busy_lock:
            _busy += 1
    released = [False]

    def release():                                   # once per capture: stop() or the child's end
        global _busy
        with _busy_lock:
            if counted and not released[0]:
                released[0] = True
                _busy = max(0, _busy - 1)

    def pump():
        while True:
            b = p.stdout.read(CHUNK * 2)             # buffered: a whole block, or the tail at EOF
            if not b:
                break
            on_chunk(b)
        release()                                    # the capture is over even if stop() never comes
        if on_end is not None:
            try:
                rc = p.wait(timeout=2)
            except Exception:
                rc = None
            try:
                on_end(rc)
            except Exception:
                pass

    t = threading.Thread(target=pump, daemon=True, name="mic-pump")
    t.start()
    warm()                                           # the next capture's helper loads meanwhile

    def stop():
        release()
        try:
            p.stdin.write(b"stop\n")
            p.stdin.flush()
        except Exception:
            pass
        t.join(1.5)
        if p.poll() is None:
            p.kill()
        p.wait()
        t.join(0.5)                                  # pump sees EOF once the child is gone
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except Exception:
                pass

    stop.proc = p
    return stop
