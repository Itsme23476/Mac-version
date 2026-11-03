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
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK = 1600           # samples per block (100 ms) — same block size the in-process stream used

_spare = None
_spare_lock = threading.Lock()


def serve():
    """Child (FILECT_MODE=mic-helper): wait for 'go', then write mic PCM to stdout until 'stop'/EOF."""
    import sounddevice as sd
    out = sys.stdout.buffer
    if not sys.stdin.readline().startswith("go"):    # parent gone / spare discarded
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


def open_mic(on_chunk):
    """Start capturing. on_chunk(bytes) runs on a reader thread for every block.
    Returns stop(): ends the capture after every block already captured has been delivered."""
    global _spare
    with _spare_lock:
        p, _spare = _spare, None
    if p is None or p.poll() is not None:
        p = _spawn()
    p.stdin.write(b"go\n")
    p.stdin.flush()

    def pump():
        while True:
            b = p.stdout.read(CHUNK * 2)             # buffered: a whole block, or the tail at EOF
            if not b:
                return
            on_chunk(b)

    t = threading.Thread(target=pump, daemon=True, name="mic-pump")
    t.start()
    warm()                                           # the next capture's helper loads meanwhile

    def stop():
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

    return stop
