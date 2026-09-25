"""
Filect Voice — dictation controller (Phase 2).

Global push-to-talk, press-to-toggle: press the shortcut to start recording,
press it again to stop; the transcript is typed into whatever app is focused.

Reuses the app's existing cross-platform global-hotkey infra
(app.ui.win_hotkey -> mac_hotkey) and focus helpers, and records/transcribes via
app.core.transcription (Grok Voice Transcribe 2.0). The reactive animation is
Phase 3 — for now there's a minimal non-focus-stealing status indicator.

Note: hold-to-talk isn't used because the reused hotkey backends are press-only
(see the reuse blueprint); press-to-toggle is the v1 gesture. Inserting text into
another app needs macOS Accessibility permission (same as the app's autofill).
"""
import logging
import time

from PySide6.QtCore import QObject, Signal, Qt, QTimer
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout
from PySide6.QtGui import QGuiApplication

from app.core.settings import settings
from app.core.transcription import VoiceRecorder

logger = logging.getLogger(__name__)

# Cross-platform hotkey + focus helpers (win_hotkey re-exports mac_hotkey on macOS).
try:
    from app.ui.win_hotkey import (
        register_global_hotkey,
        unregister_global_hotkey,
        get_foreground_hwnd,
        set_foreground_hwnd_robust,
    )
    _HOTKEY_OK = True
except Exception as e:  # pragma: no cover - only in envs without the native deps
    logger.warning(f"Dictation hotkey helpers unavailable: {e}")
    _HOTKEY_OK = False

# macOS Accessibility permission helpers (re-exported from mac_hotkey via win_hotkey).
# Without this permission, pynput typing / paste silently posts nothing.
try:
    from app.ui.win_hotkey import (
        check_accessibility_permission,
        request_accessibility_permission,
    )
    _ACCESSIBILITY_OK = True
except Exception as e:  # pragma: no cover - only in envs without the native deps
    logger.warning(f"Accessibility helpers unavailable: {e}")
    _ACCESSIBILITY_OK = False


# Phase 3 reactive animation overlay — same show_state/hide/set_level contract as
# DictationIndicator. Falls back to the minimal pill if it can't be imported.
try:
    from app.ui.dictation_overlay import DictationOverlay
    _OVERLAY_OK = True
except Exception as e:  # pragma: no cover
    logger.warning(f"Dictation overlay unavailable, using minimal indicator: {e}")
    _OVERLAY_OK = False


class DictationIndicator(QWidget):
    """Minimal, non-focus-stealing status pill. Phase 3 swaps this for the animation.

    Conforms to the indicator contract: show_state(state), hide(), set_level(level).
    """

    _STATE_TEXT = {
        "listening": "🎤  Listening…  (press shortcut again to stop)",
        "transcribing": "✍️  Transcribing…",
    }

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._label = QLabel("", self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.addWidget(self._label)
        self.setStyleSheet(
            "QWidget{background:#0A0A12;border:1px solid #7C4DFF;border-radius:12px;}"
            "QLabel{color:#FFFFFF;font-size:14px;font-weight:600;}"
        )

    def show_state(self, state: str):
        self._label.setText(self._STATE_TEXT.get(state, state))
        self.adjustSize()
        try:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.move(scr.center().x() - self.width() // 2, scr.bottom() - self.height() - 80)
        except Exception:
            pass
        self.show()
        self.raise_()

    def set_level(self, level: float):
        # No-op: the minimal pill doesn't animate. The Phase-3 overlay uses the same
        # contract method to drive its waveform.
        pass


class VoiceDictationController(QObject):
    """Owns the dictation hotkey, the recorder, and text insertion."""

    # thread-safe bridges: native hotkey callbacks (press/release) -> UI thread
    _press_sig = Signal()
    _release_sig = Signal()

    def __init__(self, main_window):
        super().__init__(main_window)
        self._mw = main_window
        self._recorder = None
        self._hotkey = None
        self._prev_pid = None
        self._state = "idle"  # "idle" | "recording" | "transcribing"
        self._accessibility_prompted = False  # prompt at most once per session
        # Gesture state — Wispr-Flow style: HOLD the key = push-to-talk (record while
        # held, transcribe on release); QUICK TAP = hands-free latch (keep recording,
        # tap again to stop). Only active when the backend reports key releases.
        self._gesture_mode = False
        self._latched = False
        self._press_time = None
        self._ignore_next_release = False
        self._key_down = False        # is the hotkey physically held right now
        self._got_second_tap = False  # a double-tap is in progress -> will latch
        self._indicator = DictationOverlay() if _OVERLAY_OK else DictationIndicator()
        # Hotkey callbacks fire on the native event thread; emit -> queued slots run on
        # the UI thread (same pattern as setup_quick_search).
        self._press_sig.connect(self._on_press, Qt.QueuedConnection)
        self._release_sig.connect(self._on_release, Qt.QueuedConnection)
        if settings.dictation_enabled:
            self.register_hotkey()

    # ---- hotkey lifecycle ----
    def register_hotkey(self):
        if not _HOTKEY_OK:
            return
        seq = settings.dictation_shortcut or "ctrl+shift+d"
        try:
            self._hotkey = register_global_hotkey(
                self._mw, seq,
                lambda: self._press_sig.emit(),
                on_released=lambda: self._release_sig.emit(),
            )
            if self._hotkey:
                handle = self._hotkey[1] if isinstance(self._hotkey[1], dict) else {}
                self._gesture_mode = bool(handle.get("supports_release"))
                logger.info(
                    f"Dictation hotkey registered: {seq} ({handle.get('kind', '?')}); "
                    f"gesture={'hold+tap-latch' if self._gesture_mode else 'tap-to-toggle'}"
                )
            else:
                logger.warning(f"Dictation hotkey registration returned None for {seq}")
        except Exception as e:
            logger.error(f"Dictation hotkey registration failed: {e}")

    def unregister_hotkey(self):
        if self._hotkey and _HOTKEY_OK:
            try:
                hotkey_id, listener, _ = self._hotkey
                unregister_global_hotkey(hotkey_id, listener)
            except Exception as e:
                logger.warning(f"Dictation hotkey unregister failed: {e}")
        self._hotkey = None

    def update_hotkey(self, seq: str):
        """Re-register after the user changes the shortcut (Phase 4 settings)."""
        self.unregister_hotkey()
        settings.set_dictation_shortcut(seq)
        if settings.dictation_enabled:
            self.register_hotkey()

    # ---- gesture handling (Wispr-Flow style) ------------------------------------
    #   HOLD the key (>= HOLD_SEC) then release  -> push-to-talk (transcribe on release)
    #   DOUBLE-TAP (2nd press within DOUBLE_TAP_SEC) -> hands-free latch; a single tap
    #     while latched stops + transcribes.
    #   A lone quick tap (no 2nd tap) is discarded, so a slightly-fast hold can't latch.
    HOLD_SEC = 0.35
    DOUBLE_TAP_SEC = 0.30

    def _on_press(self):
        """Key-down."""
        if not self._gesture_mode:
            return self._toggle()            # backend has no key-up -> tap-to-toggle
        was_down = self._key_down
        self._key_down = True
        if self._state == "transcribing":
            return                           # busy finishing the previous clip
        if self._latched:
            # A tap while hands-free recording = stop and transcribe.
            self._ignore_next_release = True
            self._latched = False
            self._stop()
            return
        if self._state == "recording":
            # Recording but not latched: either still holding (auto-repeat) or this is
            # the SECOND tap of a double-tap (key had been released -> was_down False).
            if not was_down:
                self._got_second_tap = True
            return
        # idle -> start recording (so a hold captures from the first moment)
        self._press_time = time.monotonic()
        self._got_second_tap = False
        self._start()

    def _on_release(self):
        """Key-up."""
        if not self._gesture_mode:
            return
        self._key_down = False
        if self._ignore_next_release:
            self._ignore_next_release = False
            return
        if self._state != "recording":
            return
        if self._got_second_tap:
            self._latched = True             # double-tap complete -> hands-free
            self._got_second_tap = False
            return
        held = time.monotonic() - (self._press_time or 0.0)
        if held >= self.HOLD_SEC:
            self._stop()                     # push-to-talk: transcribe on release
            return
        # A lone quick tap: wait to see if a second tap makes it a double-tap.
        QTimer.singleShot(int(self.DOUBLE_TAP_SEC * 1000), self._resolve_single_tap)

    def _resolve_single_tap(self):
        """Fires DOUBLE_TAP_SEC after a quick tap's release. If no second tap turned it
        into a double-tap (and no second tap is currently held), the lone tap isn't a real
        gesture -> discard it instead of transcribing a stray fragment."""
        if (self._state == "recording" and not self._latched
                and not self._got_second_tap and not self._key_down):
            self._cancel_recording()

    def _cancel_recording(self):
        """Stop the recorder and drop its result (used for a discarded lone tap)."""
        r = self._recorder
        self._recorder = None
        self._state = "idle"
        self._latched = False
        self._got_second_tap = False
        try:
            self._indicator.hide()
        except Exception:
            pass
        if r is not None:
            for sig in ("finished", "error"):
                try:
                    getattr(r, sig).disconnect()
                except Exception:
                    pass
            try:
                r.stop_recording()
            except Exception:
                pass

    # ---- record / transcribe / insert ----
    def _toggle(self):
        # Explicit state machine: a QThread stays isRunning() through the network
        # transcription that happens AFTER recording stops, so an isRunning() check
        # would spawn a second recorder on a rapid press. Track state ourselves so
        # exactly one recorder exists at a time.
        if self._state == "idle":
            self._start()
        elif self._state == "recording":
            self._stop()
        else:  # "transcribing"
            logger.info("still transcribing; ignoring hotkey")
            return

    def _start(self):
        try:
            self._prev_pid = get_foreground_hwnd() if _HOTKEY_OK else None
        except Exception:
            self._prev_pid = None
        self._recorder = VoiceRecorder()
        self._recorder.finished.connect(self._on_text)
        self._recorder.error.connect(self._on_error)
        self._recorder.level.connect(self._indicator.set_level)
        self._state = "recording"
        self._recorder.start()
        self._indicator.show_state("listening")

    def _stop(self):
        if self._recorder:
            self._recorder.stop_recording()
        self._state = "transcribing"
        self._indicator.show_state("transcribing")

    def _on_text(self, text: str):
        text = (text or "").strip()
        logger.info(f"Transcript received: {len(text)} chars")
        self._indicator.hide()
        self._state = "idle"
        self._latched = False
        self._ignore_next_release = False
        self._got_second_tap = False
        if not text:
            self._status("No speech detected.")
            return
        # Do NOT touch focus: the overlay is a non-activating panel, so the app the
        # user was typing in is still frontmost. We paste straight into it (exactly
        # like the quick-search auto-popup). The old set_foreground_hwnd_robust()
        # restore is what caused the visible "switch to Filect and back".
        QTimer.singleShot(120, lambda: self._insert_text(text))

    def _on_error(self, msg: str):
        logger.warning(f"Dictation error: {msg}")
        self._indicator.hide()
        self._state = "idle"
        self._latched = False
        self._ignore_next_release = False
        self._got_second_tap = False
        self._status(msg or "Dictation failed.")

    def _insert_text(self, text: str):
        # macOS: pynput typing / Cmd+V paste silently no-op without Accessibility
        # permission. Warn loudly and prompt once so it never looks like a silent fail.
        # HARD REQUIREMENT: synthesizing Cmd+V (or pynput typing) into another app needs
        # the Accessibility permission. Without it macOS SILENTLY DROPS the keystroke —
        # the clipboard gets set but nothing pastes, and CGEventPost doesn't error, so we
        # must not claim success. Gate on it, and when missing, register+prompt and leave
        # the transcript on the clipboard so the user can ⌘V manually.
        if _ACCESSIBILITY_OK:
            trusted = False
            try:
                trusted = check_accessibility_permission()
            except Exception as e:
                logger.warning(f"Accessibility check failed: {e}")
            if not trusted:
                logger.warning(
                    "Accessibility NOT granted — cannot auto-paste; leaving transcript "
                    "on clipboard for manual paste"
                )
                self._set_clipboard(text)  # so the user can ⌘V it right now
                if not self._accessibility_prompted:
                    self._accessibility_prompted = True
                    try:
                        request_accessibility_permission()  # registers app + native prompt
                    except Exception as e:
                        logger.warning(f"Accessibility prompt failed: {e}")
                self._status(
                    "Enable Filect under Privacy & Security → Accessibility to auto-paste. "
                    "Transcript copied — press ⌘V to paste it now."
                )
                return

        # Accessibility OK: clipboard + Cmd+V — the SAME mechanism the quick-search
        # auto-popup uses (autofill_via_clipboard_paste). Because the overlay is
        # non-activating, focus stays in the user's app and the paste lands there. We
        # save/restore the clipboard so dictation doesn't clobber it.
        try:
            from app.ui.mac_hotkey import autofill_via_clipboard_paste
            prev_clip = self._get_clipboard()
            if autofill_via_clipboard_paste(text):
                logger.info(f"Inserted {len(text)} chars via clipboard paste")
                self._status("Dictation inserted.")
                if prev_clip is not None:
                    QTimer.singleShot(400, lambda: self._set_clipboard(prev_clip))
                return
            logger.warning("Clipboard paste returned False; trying pynput type")
        except Exception as e:
            logger.warning(f"Clipboard paste failed ({e}); trying pynput type")
        # Fallback: type at the cursor.
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
            logger.info(f"Inserted {len(text)} chars via pynput")
            self._status("Dictation inserted.")
        except Exception as e:
            logger.error(f"Text insertion failed via all paths: {e}")
            self._status("Could not insert text.")

    @staticmethod
    def _get_clipboard():
        try:
            from AppKit import NSPasteboard, NSStringPboardType
            return NSPasteboard.generalPasteboard().stringForType_(NSStringPboardType)
        except Exception:
            return None

    @staticmethod
    def _set_clipboard(value: str):
        try:
            from AppKit import NSPasteboard, NSStringPboardType
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(value, NSStringPboardType)
        except Exception:
            pass

    def _status(self, msg: str):
        try:
            if hasattr(self._mw, "status_bar") and self._mw.status_bar is not None:
                self._mw.status_bar.showMessage(f"Voice: {msg}", 4000)
        except Exception:
            pass

    def cleanup(self):
        self.unregister_hotkey()
        try:
            if self._recorder and self._recorder.isRunning():
                self._recorder.stop_recording()
                self._recorder.wait(1000)
        except Exception:
            pass
