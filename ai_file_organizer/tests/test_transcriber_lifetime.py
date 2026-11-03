"""A cancelled capture (caller drops its StreamingTranscriber while it runs) must not abort the app
with "QThread: Destroyed while thread is still running". No mic, no network.
Run: venv311/bin/python ai_file_organizer/tests/test_transcriber_lifetime.py"""
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication  # noqa: E402
from app.core import transcription as T  # noqa: E402


async def _quick_session(self):
    import asyncio
    await asyncio.sleep(0.2)          # still running when the caller lets go of it
    return "hi", True


def test_cancelled_capture_does_not_abort(monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr(T.StreamingTranscriber, "_session", _quick_session)
    else:
        T.StreamingTranscriber._session = _quick_session
    app = QCoreApplication.instance() or QCoreApplication([])
    for _ in range(20):
        r = T.StreamingTranscriber()
        r.start()
        del r                             # what _cancel_recording does
        gc.collect()
    deadline = time.monotonic() + 5
    while any(not t.isFinished() for t in T._RUNNING) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert all(t.isFinished() for t in T._RUNNING)
    r = T.StreamingTranscriber(); r.start(); r.wait(2000)   # next start prunes the finished ones
    assert len(T._RUNNING) == 1


if __name__ == "__main__":
    test_cancelled_capture_does_not_abort()
    print("test_transcriber_lifetime OK")
