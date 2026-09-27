#!/usr/bin/env python3
"""
Filect Voice Dictation — behavior check (read me top-to-bottom).

This is ONE script you can read to see exactly what dictation is supposed to do, and
run to confirm the real code actually does it. Each check below has a plain-English
description of the expected behavior, followed by the assertions that prove it.

It exercises the REAL controller (app.ui.dictation) and the REAL transcription client
(app.core.transcription). Only the things that need a live Mac/mic/network are faked:
  - the microphone recorder (no audio is captured),
  - the network call to the transcription server,
  - the macOS Accessibility permission check,
  - the actual key-injection / clipboard (so nothing is typed into your other apps).
So it runs offline in about a second and changes nothing on your machine.

Run it:
    cd ~/Developer/Mac_app
    venv311/bin/python voice_dictation_check.py
"""
import os
import sys
import time

# Headless Qt: the controller uses QTimer/QObject but we never show a real window.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make the `app` package (inside ai_file_organizer/) importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "ai_file_organizer"))

from unittest.mock import MagicMock, patch
from PySide6.QtCore import QObject, Signal, QCoreApplication
from PySide6.QtWidgets import QApplication

qapp = QApplication.instance() or QApplication([])  # note: `qapp`, not `app` (name clash)

import app.ui.dictation as dictation
from app.core import transcription


# --- a stand-in microphone: same signals as the real VoiceRecorder, no audio -------
class FakeRecorder(QObject):
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


def _new_controller():
    """A dictation controller with the native hotkey/focus calls disabled and a fake
    status bar + indicator, so nothing touches the real desktop."""
    dictation._HOTKEY_OK = False           # no real Carbon hotkey / focus switching
    mw = QObject()
    mw.status_bar = MagicMock()
    c = dictation.VoiceDictationController(mw)
    c._indicator = MagicMock()             # don't pop a real overlay window
    return c


# ======================================================================================
# The checks. Each function's docstring is the behavior spec; the asserts are the proof.
# ======================================================================================

def check_hotkey_toggles_recording():
    """Pressing the shortcut starts recording; pressing it again stops and moves to
    transcribing. (This is the push-to-talk toggle gesture.)"""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        assert c._state == "idle", "should start idle"
        c._toggle()
        assert c._recorder.isRunning() and c._state == "recording", "1st press -> recording"
        c._toggle()
        assert c._recorder.stopped and c._state == "transcribing", "2nd press -> transcribing"
        c.cleanup()


def check_finished_transcript_is_inserted():
    """When transcription finishes, the exact recognized text is handed to the
    insert step (what gets typed into your app)."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._insert_text = MagicMock()
        c._toggle()                                  # wires the recorder's finished -> _on_text
        c._recorder.finished.emit("hello world")
        deadline = time.time() + 1.5                 # _on_text defers insert via QTimer
        while not c._insert_text.called and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        c._insert_text.assert_called_once_with("hello world")
        c.cleanup()


def check_paste_when_accessibility_granted():
    """With Accessibility GRANTED, the transcript is pasted using the same clipboard+
    Cmd-V mechanism the quick-search popup uses (autofill_via_clipboard_paste)."""
    c = _new_controller()
    c._get_clipboard = MagicMock(return_value=None)  # skip clipboard save/restore
    dictation._ACCESSIBILITY_OK = True
    with patch.object(dictation, "check_accessibility_permission", return_value=True), \
         patch("app.ui.mac_hotkey.autofill_via_clipboard_paste", return_value=True) as paste:
        c._insert_text("meeting notes for Friday")
    paste.assert_called_once_with("meeting notes for Friday")
    c.cleanup()


def check_no_silent_failure_when_accessibility_denied():
    """THE KEY FIX. With Accessibility DENIED we must NOT pretend it worked: no paste
    is attempted, the transcript is placed on the clipboard (so you can Cmd-V it), and
    the user is told to grant access."""
    c = _new_controller()
    c._set_clipboard = MagicMock()                   # capture what we copy
    c._status = MagicMock()                          # capture the user-facing message
    dictation._ACCESSIBILITY_OK = True
    with patch.object(dictation, "check_accessibility_permission", return_value=False), \
         patch.object(dictation, "request_accessibility_permission"), \
         patch("app.ui.mac_hotkey.autofill_via_clipboard_paste") as paste:
        c._insert_text("do not lose this text")
    paste.assert_not_called()                                    # no fake success
    c._set_clipboard.assert_called_once_with("do not lose this text")  # kept for Cmd-V
    msg = " ".join(str(a) for a in c._status.call_args.args)
    assert "Accessibility" in msg, f"user should be told to grant Accessibility, got: {msg!r}"
    c.cleanup()


def check_empty_transcript_inserts_nothing():
    """Silence / an empty transcript types nothing (no stray characters)."""
    c = _new_controller()
    c._insert_text = MagicMock()
    c._on_text("   ")
    QCoreApplication.processEvents()
    c._insert_text.assert_not_called()
    c.cleanup()


def check_press_during_transcription_is_ignored():
    """Pressing the shortcut again while a transcription is in flight is ignored, so
    you can't accidentally start a second overlapping recording."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._toggle()                      # recording
        first = c._recorder
        c._toggle()                      # -> transcribing
        c._toggle()                      # should be ignored
        assert c._recorder is first and c._state == "transcribing"
        c.cleanup()


def check_hold_to_talk():
    """HOLD the shortcut (press, keep held, release) = push-to-talk: it records while
    held and transcribes the moment you let go."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._gesture_mode = True                       # backend reports key releases
        c._on_press()
        assert c._state == "recording" and not c._latched, "press starts recording"
        c._press_time -= 1.0                          # pretend the key was held ~1s
        c._on_release()
        assert c._state == "transcribing" and c._recorder.stopped, "release after hold -> transcribe"
        c.cleanup()


def check_double_tap_latches_hands_free():
    """A DOUBLE-TAP (two quick taps) latches hands-free recording — it keeps going after
    you release — and a later single tap stops and transcribes. One tap alone does NOT
    latch (so a slightly-fast hold can't accidentally lock you in)."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._gesture_mode = True
        c._on_press(); c._on_release()                # tap 1
        assert c._state == "recording" and not c._latched, "one tap alone does not latch yet"
        c._on_press()                                 # tap 2 (within the window)
        assert c._got_second_tap, "second quick press is recognized as a double-tap"
        c._on_release()
        assert c._state == "recording" and c._latched, "double-tap latches hands-free"
        c._on_press()                                 # single tap stops it
        assert c._state == "transcribing" and c._recorder.stopped, "tap while latched stops + transcribes"
        c.cleanup()


def check_lone_tap_is_discarded():
    """A LONE quick tap (no second tap) is discarded when the double-tap window expires —
    it must not latch and must not transcribe a stray fragment."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._gesture_mode = True
        c._on_press(); c._on_release()                # one quick tap
        assert c._state == "recording" and not c._latched, "recording, awaiting a possible 2nd tap"
        c._resolve_single_tap()                       # simulate the double-tap window expiring
        assert c._state == "idle" and not c._latched, "lone tap discarded -> idle, no latch"
        c.cleanup()


def check_shift_selects_search_mode():
    """Holding Shift at press (Fn+Shift) selects SEARCH mode: the transcript is routed to
    the quick-search popup via _do_search, NOT pasted. Fn alone stays dictate."""
    with patch.object(dictation, "VoiceRecorder", FakeRecorder):
        c = _new_controller()
        c._gesture_mode = True
        c._do_search = MagicMock()
        c._insert_text = MagicMock()
        c._on_press(shift=True)                       # Fn+Shift -> search
        assert c._mode == "search", "Shift at press selects search mode"
        c._press_time -= 1.0                          # simulate a hold
        c._on_release()                               # -> transcribing
        c._recorder.finished.emit("budget spreadsheet")
        deadline = time.time() + 1.5
        while not c._do_search.called and time.time() < deadline:
            QCoreApplication.processEvents(); time.sleep(0.02)
        c._do_search.assert_called_once_with("budget spreadsheet")
        c._insert_text.assert_not_called()            # search must not paste
        c.cleanup()


def check_transcription_results_map_correctly():
    """The transcription client turns server responses into the right outcomes:
    success -> trimmed text; and each error into a clear code the UI can act on."""
    import tempfile
    clip = os.path.join(tempfile.gettempdir(), "vdc_probe.wav")
    with open(clip, "wb") as f:
        f.write(b"RIFF0000WAVE")

    class Resp:
        def __init__(self, code, js):
            self.status_code, self._js, self.text = code, js, str(js)
        def json(self):
            return self._js

    with patch.object(transcription, "_get_auth_token", return_value="tok"):
        with patch.object(transcription.requests, "post", return_value=Resp(200, {"text": " hi ", "duration": 3})):
            r = transcription.transcribe_audio(clip)
            assert r["ok"] and r["text"] == "hi" and r["duration"] == 3, "200 -> trimmed text"
        with patch.object(transcription.requests, "post", return_value=Resp(403, {})):
            assert transcription.transcribe_audio(clip)["error"] == "no_subscription", "403 -> no_subscription"
        with patch.object(transcription.requests, "post", return_value=Resp(429, {})):
            assert transcription.transcribe_audio(clip)["error"] == "rate_limited", "429 -> rate_limited"
        with patch.object(transcription.requests, "post", return_value=Resp(500, {})):
            assert transcription.transcribe_audio(clip)["error"] == "provider", "500 -> provider"
    with patch.object(transcription, "_get_auth_token", return_value=None):
        assert transcription.transcribe_audio(clip)["error"] == "not_authenticated", "no token -> not_authenticated"
    os.unlink(clip)


CHECKS = [
    check_hotkey_toggles_recording,
    check_finished_transcript_is_inserted,
    check_paste_when_accessibility_granted,
    check_no_silent_failure_when_accessibility_denied,
    check_empty_transcript_inserts_nothing,
    check_press_during_transcription_is_ignored,
    check_hold_to_talk,
    check_double_tap_latches_hands_free,
    check_lone_tap_is_discarded,
    check_shift_selects_search_mode,
    check_transcription_results_map_correctly,
]


def main():
    print("\nFilect Voice Dictation — behavior check\n" + "=" * 44)
    passed = 0
    for fn in CHECKS:
        desc = " ".join((fn.__doc__ or fn.__name__).split())
        try:
            fn()
            passed += 1
            print(f"\n✅ PASS  {fn.__name__}\n   {desc}")
        except AssertionError as e:
            print(f"\n❌ FAIL  {fn.__name__}\n   {desc}\n   -> {e}")
        except Exception as e:
            print(f"\n\U0001f4a5 ERROR {fn.__name__}\n   {desc}\n   -> {type(e).__name__}: {e}")
    print("\n" + "=" * 44)
    print(f"{passed}/{len(CHECKS)} behaviors verified.")
    print("Note: real key-injection into other apps needs macOS Accessibility and can't")
    print("be checked here — this proves the logic; the app itself proves the live paste.\n")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
