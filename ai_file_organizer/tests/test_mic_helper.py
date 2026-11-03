"""mic_helper parent side, with fake children (no mic, no PortAudio).
Run: venv311/bin/python ai_file_organizer/tests/test_mic_helper.py"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import mic_helper  # noqa: E402

BLOCK = b"\x01\x02" * mic_helper.CHUNK
# well-behaved: 5 blocks after "go", then exit on "stop"
GOOD = ("import sys,os\n"
        "if sys.stdin.readline().startswith('go'):\n"
        "    for _ in range(5): sys.stdout.buffer.write(b'\\x01\\x02'*1600); sys.stdout.buffer.flush()\n"
        "    sys.stdin.readline()\n"
        "os._exit(0)\n")
# wedged: streams, then ignores "stop" forever (what a deadlocked PortAudio child would do)
HUNG = ("import sys,time\n"
        "sys.stdin.readline(); sys.stdout.buffer.write(b'\\x01\\x02'*1600); sys.stdout.buffer.flush()\n"
        "time.sleep(60)\n")


def _fake(code):
    return lambda: subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _capture(code, hold=0.3):
    mic_helper._spawn, mic_helper._spare = _fake(code), None
    got = []
    stop = mic_helper.open_mic(got.append)
    time.sleep(hold)
    t0 = time.monotonic()
    stop()
    return got, time.monotonic() - t0



def test_mic_helper_parent():
    real_spawn = mic_helper._spawn
    try:
        _check()
    finally:
        mic_helper._spawn, mic_helper._spare = real_spawn, None


def _check():
    got, took = _capture(GOOD)
    assert b"".join(got) == BLOCK * 5, "every block must reach the parent"
    assert took < 1.0, took

    got, took = _capture(HUNG)
    assert b"".join(got) == BLOCK
    assert took < 3.0, f"a wedged child must be killed, not waited on ({took:.1f}s)"

    # the spare is reused, and a fresh one is loaded for the next capture
    mic_helper._spawn, mic_helper._spare = _fake(GOOD), None
    mic_helper.warm()
    spare = mic_helper._spare
    stop = mic_helper.open_mic(lambda b: None)
    assert mic_helper._spare is not None and mic_helper._spare is not spare
    stop()
    assert spare.poll() is not None, "used helper has exited"
    mic_helper._spare.kill()


if __name__ == "__main__":
    test_mic_helper_parent()
    print("test_mic_helper OK")
