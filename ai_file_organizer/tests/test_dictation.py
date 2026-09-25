"""
Phase 2 tests — Filect Voice dictation controller + transcription client.

Everything native/network is mocked, so this runs offline with just PySide6:
  - VoiceRecorder is replaced with a fake that emits the same signals
  - the global-hotkey helpers are unavailable in this env (_HOTKEY_OK False), so
    no native calls happen
  - text insertion (_insert_text) is mocked
  - requests.post is mocked for the transcribe result-mapping tests

Run: ../venv/bin/python -m pytest tests/test_dictation.py -q
"""
import os
import sys
import time

import pytest
from unittest.mock import MagicMock, patch

# Make the `app` package importable (tests/ is inside ai_file_organizer/).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from PySide6.QtCore import QObject, Signal, QCoreApplication
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeRecorder(QObject):
    """Stand-in for VoiceRecorder — same signal surface, no audio/network."""
    finished = Signal(str)
    error = Signal(str)
    recording_stopped = Signal()
    level = Signal(float)

    def __init__(self, *a, **k):
        super().__init__()
        self._running = False
        self.stopped = False

    def start(self):
        self._running = True

    def isRunning(self):
        return self._running

    def stop_recording(self):
        self._running = False
        self.stopped = True

    def wait(self, *a):
        return True


def _make_controller():
    import app.ui.dictation as d
    d._HOTKEY_OK = False          # isolate tests from real Carbon hotkey / focus calls
    mw = QObject()
    mw.status_bar = MagicMock()
    c = d.VoiceDictationController(mw)
    c._indicator = MagicMock()   # don't pop a real window during tests
    return d, c


def test_toggle_starts_and_stops_recording(qapp):
    import app.ui.dictation as d
    with patch.object(d, "VoiceRecorder", FakeRecorder):
        _, c = _make_controller()
        assert c._recorder is None
        assert c._state == "idle"
        c._toggle()                      # first press -> start
        assert c._recorder is not None and c._recorder.isRunning()
        assert c._state == "recording"
        c._indicator.show_state.assert_called_with("listening")
        c._toggle()                      # second press -> stop
        assert c._recorder.stopped is True
        assert c._state == "transcribing"
        c.cleanup()


def test_toggle_ignored_while_transcribing(qapp):
    """A press during transcription must NOT spawn a second recorder (BUG 1)."""
    import app.ui.dictation as d
    with patch.object(d, "VoiceRecorder", FakeRecorder):
        _, c = _make_controller()
        c._toggle()                      # idle -> recording
        first = c._recorder
        c._toggle()                      # recording -> transcribing (stop)
        assert c._state == "transcribing"
        c._toggle()                      # transcribing -> ignored, no new recorder
        assert c._recorder is first
        assert c._state == "transcribing"
        c.cleanup()


def test_state_resets_to_idle_after_result(qapp):
    """Both finish and error paths return the controller to idle so it can start again."""
    import app.ui.dictation as d
    with patch.object(d, "VoiceRecorder", FakeRecorder):
        _, c = _make_controller()
        c._insert_text = MagicMock()
        c._toggle()                      # recording
        c._stop()                        # transcribing
        c._on_text("done")               # finish -> idle
        assert c._state == "idle"
        c._on_error("boom")              # error -> idle (and stays idle)
        assert c._state == "idle"
        c.cleanup()


def test_finished_schedules_insert_with_text(qapp):
    import app.ui.dictation as d
    with patch.object(d, "VoiceRecorder", FakeRecorder):
        _, c = _make_controller()
        c._insert_text = MagicMock()
        c._toggle()                      # start -> creates recorder + wires _on_text
        c._recorder.finished.emit("hello world")
        # _on_text defers _insert_text via QTimer.singleShot(120)
        deadline = time.time() + 1.5
        while not c._insert_text.called and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        c._insert_text.assert_called_once_with("hello world")
        c.cleanup()


def test_empty_transcript_does_not_insert(qapp):
    import app.ui.dictation as d
    with patch.object(d, "VoiceRecorder", FakeRecorder):
        _, c = _make_controller()
        c._insert_text = MagicMock()
        c._on_text("   ")               # whitespace-only -> nothing to type
        QCoreApplication.processEvents()
        c._insert_text.assert_not_called()
        c.cleanup()


def test_settings_persist_and_normalize(qapp):
    from app.core.settings import settings
    settings.set_dictation_shortcut("  CMD+Shift+K ")
    assert settings.dictation_shortcut == "cmd+shift+k"     # trimmed + lowercased
    settings.set_dictation_enabled(False)
    assert settings.dictation_enabled is False
    # restore sane defaults so we don't leave the app disabled
    settings.set_dictation_enabled(True)
    settings.set_dictation_shortcut("ctrl+shift+d")


def test_transcribe_audio_result_mapping(qapp, tmp_path):
    from app.core import transcription as t
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFF0000WAVE")

    class Resp:
        def __init__(self, code, js):
            self.status_code = code
            self._js = js
            self.text = str(js)
        def json(self):
            return self._js

    with patch.object(t, "_get_auth_token", return_value="tok"):
        with patch.object(t.requests, "post", return_value=Resp(200, {"text": " hi ", "duration": 3})):
            r = t.transcribe_audio(str(clip))
            assert r["ok"] and r["text"] == "hi" and r["duration"] == 3
        with patch.object(t.requests, "post", return_value=Resp(403, {})):
            assert t.transcribe_audio(str(clip))["error"] == "no_subscription"
        with patch.object(t.requests, "post", return_value=Resp(429, {})):
            assert t.transcribe_audio(str(clip))["error"] == "rate_limited"
        with patch.object(t.requests, "post", return_value=Resp(500, {})):
            assert t.transcribe_audio(str(clip))["error"] == "provider"
    with patch.object(t, "_get_auth_token", return_value=None):
        assert t.transcribe_audio(str(clip))["error"] == "not_authenticated"
