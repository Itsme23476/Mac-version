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
from app.core.transcription import StreamingTranscriber

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

    # thread-safe bridges: native hotkey callbacks (press/release) -> UI thread.
    # _press_sig carries (shift, option) held at press: Fn = dictate, Fn+Shift = search,
    # Fn+Option = organize.
    _press_sig = Signal(bool, bool)
    _release_sig = Signal()
    _search_ready = Signal(str, str)  # (spoken text, distilled query) -> open popup (UI thread)
    _final_ready = Signal(str)        # final dictation text (after optional AI cleanup) -> paste
    dictation_saved = Signal()        # a dictation was saved to History -> refresh the card

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
        self._mode = "dictate"        # "dictate" | "search" | "organize" — set at capture start
        self.organize_controller = None   # VoiceOrganizeController, injected by main_window
        self._organize_active = False     # (legacy) unused; mode is now decided at release
        self._mods_seen = {"shift": False, "option": False}  # modifiers seen during a capture
        self._mod_timer = None            # QTimer that polls modifiers while recording
        self._tx_watchdog = None          # QTimer: auto-recovers a stuck "transcribing" state
        self._indicator = DictationOverlay() if _OVERLAY_OK else DictationIndicator()
        # Hotkey callbacks fire on the native event thread; emit -> queued slots run on
        # the UI thread (same pattern as setup_quick_search).
        self._press_sig.connect(self._on_press, Qt.QueuedConnection)
        self._release_sig.connect(self._on_release, Qt.QueuedConnection)
        self._search_ready.connect(self._open_search, Qt.QueuedConnection)
        self._final_ready.connect(self._on_final, Qt.QueuedConnection)
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
                lambda shift=False, option=False: self._press_sig.emit(bool(shift), bool(option)),
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

    def _on_press(self, shift=False, option=False):
        """Key-down. Modifiers held at press pick the mode: Fn = dictate, Fn+Shift = search,
        Fn+Option = organize."""
        if not self._gesture_mode:
            return self._toggle()            # backend has no key-up -> tap-to-toggle
        was_down = self._key_down
        self._key_down = True
        # Re-read the LIVE modifier state here (UI thread, a few ms after the tap). For a
        # held chord like Fn+Option, the Option bit is often not set yet at the exact
        # Fn-down instant the tap samples, but IS set by now — so OR it in. Makes mode
        # selection reliable no matter how simultaneously the two keys are pressed.
        live_shift = live_option = None
        try:
            from Quartz import (CGEventSourceFlagsState,
                                kCGEventSourceStateCombinedSessionState)
            _f = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
            live_shift = bool(_f & 0x20000)    # kCGEventFlagMaskShift
            live_option = bool(_f & 0x80000)   # kCGEventFlagMaskAlternate (Option / ⌥)
            shift = shift or live_shift
            option = option or live_option
        except Exception:
            pass
        logger.info(f"[voice] Fn press: live(shift={live_shift} option={live_option}) -> "
                    f"shift={shift} option={option} state={self._state} "
                    f"organize_ctrl={self.organize_controller is not None}")
        if self._state == "transcribing":
            # A press while a (possibly stuck/laggy) transcription is still in flight =
            # CANCEL it: drop the pending result and return to idle, so a hang never traps
            # the user. They tap again to start a fresh capture.
            logger.info("hotkey during transcription -> cancelling the in-flight request")
            self._cancel_recording()
            return
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
        # idle -> start a fresh capture. The MODE (dictate/search/organize) is NOT locked
        # here: the modifier is usually added a beat after Fn, so locking now always misses
        # it. Instead we watch modifiers for the whole hold (_poll_mods) and decide in
        # _stop(). Seed with whatever is already down this instant.
        self._mods_seen = {"shift": bool(shift), "option": bool(option)}
        self._mode = "dictate"
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
            try:
                self._indicator.set_latched(True)   # distinct cue: locked on, no need to hold
            except Exception:
                pass
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
        if self._mod_timer is not None:
            self._mod_timer.stop()
            self._mod_timer = None
        self._stop_watchdog()
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
        # StreamingTranscriber streams audio live so the final text is ready the
        # instant the key is released; it falls back to the batch path (VoiceRecorder's
        # behaviour) on any streaming problem, so nothing about dictation can break.
        self._recorder = StreamingTranscriber(terms=settings.dictation_custom_terms,
                                              language=(settings.dictation_language or None))
        self._recorder.finished.connect(self._on_text)
        self._recorder.error.connect(self._on_error)
        self._recorder.level.connect(self._indicator.set_level)
        if hasattr(self._recorder, "capped"):
            self._recorder.capped.connect(self._on_capped)
        self._state = "recording"
        self._recorder.start()
        # Poll modifiers for the whole hold so mode selection is independent of press
        # order/timing (user usually adds Shift/Option a beat after Fn).
        self._mod_timer = QTimer(self)
        self._mod_timer.setInterval(70)
        self._mod_timer.timeout.connect(self._poll_mods)
        self._mod_timer.start()
        try:
            self._indicator.set_mode(self._mode)   # recolor the pill: search vs dictate
        except Exception:
            pass
        try:
            self._indicator.set_latched(False)
        except Exception:
            pass
        self._indicator.show_state("listening")

    def _poll_mods(self):
        """Sample the live modifier state while recording; remember any Shift/Option seen."""
        try:
            from Quartz import (CGEventSourceFlagsState,
                                kCGEventSourceStateCombinedSessionState)
            f = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
        except Exception:
            return
        if f & 0x20000:
            self._mods_seen["shift"] = True
        if f & 0x80000:
            self._mods_seen["option"] = True
        # Live cue on the pill so you can SEE the mode while holding: folder=organize,
        # magnifier=search, plain=dictate.
        provisional = ("organize" if self._mods_seen["option"]
                       else "search" if self._mods_seen["shift"] else "dictate")
        try:
            self._indicator.set_mode(provisional)
        except Exception:
            pass

    def _stop_watchdog(self):
        if self._tx_watchdog is not None:
            self._tx_watchdog.stop()
            self._tx_watchdog = None

    def _on_transcribe_timeout(self):
        # Safety net: if a transcription never returns (stuck recorder / hung network), don't
        # leave the pill spinning forever — drop it and let the user retry.
        if self._state == "transcribing":
            logger.warning("[voice] transcription watchdog fired — auto-cancelling a stuck transcription")
            self._cancel_recording()
            self._status("Transcription timed out — tap to try again.")

    def _stop(self):
        self._poll_mods()                          # one last sample before deciding
        if self._mod_timer is not None:
            self._mod_timer.stop()
            self._mod_timer = None
        # Decide the mode from whatever modifier was held at ANY point during the hold:
        # Option -> organize, else Shift -> search, else dictate.
        if self._mods_seen.get("option"):
            self._mode = "organize"
        elif self._mods_seen.get("shift"):
            self._mode = "search"
        else:
            self._mode = "dictate"
        logger.info(f"[voice] mode decided: {self._mode} (mods_seen={self._mods_seen})")
        if self._recorder:
            self._recorder.stop_recording()
        self._state = "transcribing"
        self._indicator.show_state("transcribing")
        self._stop_watchdog()
        self._tx_watchdog = QTimer(self)
        self._tx_watchdog.setSingleShot(True)
        self._tx_watchdog.timeout.connect(self._on_transcribe_timeout)
        self._tx_watchdog.start(25000)   # transcription normally takes ~1-3s

    def _on_text(self, text: str):
        self._stop_watchdog()
        text = (text or "").strip()
        logger.info(f"Transcript received: {len(text)} chars")
        self._state = "idle"
        self._latched = False
        self._ignore_next_release = False
        self._got_second_tap = False
        if not text:
            self._indicator.hide()
            self._status("No speech detected.")
            return
        if self._mode == "organize":
            self._indicator.hide()
            if self.organize_controller is not None:
                try:
                    self.organize_controller.start_from_transcript(text)
                except Exception as e:
                    logger.error(f"voice organize start failed: {e}")
            else:
                self._status("Voice organize unavailable.")
            return
        if self._mode == "search":
            # Keep the pill up (transcribing dots) through the distill round-trip;
            # _open_search hides it the instant the results popup opens, so there's
            # never a blank moment between releasing and the popup appearing.
            self._do_search(text)
            return
        # Optional Polishing (Voice > Polishing: none/light/polished): polish the
        # transcript off the UI thread (it's a network call), then paste. none = raw.
        level = getattr(settings, 'dictation_polish_level', 'none')
        if level in ("light", "polished"):
            # Keep the pill up (processing) through the polish round-trip so there's
            # visible feedback instead of a dead ~2s wait; _on_final hides it.
            try:
                self._indicator.show_state("transcribing")
            except Exception:
                pass
            self._status("Polishing…")
            import threading

            def _clean():
                cleaned = text
                try:
                    from app.core.transcription import clean_transcript
                    # Pass Custom Words so cleanup fixes/keeps them (Filect != Firefox).
                    cleaned = clean_transcript(text, level, settings.dictation_custom_terms)
                except Exception as e:
                    logger.warning(f"Polishing failed: {e}")
                self._final_ready.emit(cleaned or text)
            threading.Thread(target=_clean, daemon=True).start()
            return
        # Keep the pill in its "transcribing" dots state (set in _stop) right up until
        # _on_final has actually pasted — then it shows a "done" check. No empty gap.
        self._final_ready.emit(text)

    def _on_final(self, text: str):
        """Final dictation text (raw, or AI-polished). The dots keep animating (kept from
        release) with NOTHING blocking the UI thread, then the pill is cut the instant we
        paste. History save + the Voice-page refresh run AFTER the paste on a background
        thread — that synchronous file I/O, run mid-animation, was what froze the dots.
        Do NOT touch focus: the overlay is a non-activating panel, so the user's app stays
        frontmost and we paste straight into it."""
        text = (text or "").strip()
        if not text:
            self._indicator.hide()
            return

        def _do_insert():
            # Cut the animation the INSTANT we paste (hide is instant — no window fade),
            # then paste. Nothing heavy ran on the UI thread while the dots were showing,
            # so there's no freeze — the pill just vanishes cleanly as the text lands.
            try:
                self._indicator.hide()
            except Exception:
                pass
            self._insert_text(text)
            # History + Voice-page refresh AFTER the paste, OFF the UI thread (this file
            # I/O froze the dots when it ran mid-animation). emit() is queued to the UI
            # thread; it runs after add() so the Voice card sees the new entry.
            def _persist():
                try:
                    from app.core import dictation_history
                    dictation_history.add(text)
                except Exception as e:
                    logger.warning(f"history save failed: {e}")
                try:
                    self.dictation_saved.emit()
                except Exception:
                    pass
            import threading
            threading.Thread(target=_persist, daemon=True, name="dictation-history").start()

        QTimer.singleShot(40, _do_insert)

    def _on_error(self, msg: str):
        self._stop_watchdog()
        logger.warning(f"Dictation error: {msg}")
        self._indicator.hide()
        self._state = "idle"
        self._latched = False
        self._ignore_next_release = False
        self._got_second_tap = False
        self._status(msg or "Dictation failed.")

    def _on_capped(self):
        """The 10-min safety cap stopped a very long dictation. Everything spoken so far
        is still pasted + saved (the normal finalize path runs via finished/_on_text) —
        this just tells the user why recording stopped and that nothing was lost. Shown as
        a macOS banner because the user is typically dictating into another app."""
        logger.info("[voice] max-duration cap reached; notifying user (text is still saved)")
        self._status("10-minute voice limit reached — your text was saved.")

        def _notify():
            try:
                import subprocess
                subprocess.run(
                    ["osascript", "-e",
                     'display notification "Reached the 10-minute limit. Your text was '
                     'pasted and saved — press Fn to keep dictating." with title "Filect Voice"'],
                    capture_output=True, timeout=3)
            except Exception:
                pass
        import threading
        threading.Thread(target=_notify, daemon=True, name="voice-cap-notify").start()

    def _do_search(self, raw_query: str):
        """Voice search (Fn+Shift): distill the spoken sentence into keywords via the LLM
        OFF the UI thread, then open the popup. Falls back to the raw transcript if the
        distiller fails or the user is over the monthly cap."""
        import threading
        self._status("Preparing search…")

        def worker():
            cleaned = None
            try:
                from app.core.transcription import distill_search_query
                cleaned = distill_search_query(raw_query)
            except Exception as e:
                logger.warning(f"Query distillation failed: {e}")
            self._search_ready.emit(raw_query, cleaned or raw_query)

        threading.Thread(target=worker, daemon=True, name="filect-distill").start()

    def _open_search(self, spoken_text: str, search_query: str):
        """Open the quick-search popup showing what the user SAID, but searching on the
        distilled keywords (UI thread). Same search + reveal-in-Finder as typing."""
        try:
            qo = getattr(self._mw, "quick_overlay", None)
            if qo is None:
                logger.warning("Voice search: quick_overlay not available")
                self._indicator.hide()
                self._status("Search isn't available.")
                return
            self._indicator.hide()          # hand off from the pill to the results popup
            qo.show_centered_bottom()
            qo.run_voice_query(spoken_text, search_query)   # display spoken, search distilled
            logger.info(f"Voice search: said {spoken_text!r} -> searched {search_query!r}")
            self._status(f"Searching: {search_query}")
        except Exception as e:
            logger.error(f"Voice search failed: {e}")
            self._status("Search failed.")

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
                return True
            logger.warning("Clipboard paste returned False; trying pynput type")
        except Exception as e:
            logger.warning(f"Clipboard paste failed ({e}); trying pynput type")
        # Fallback: type at the cursor.
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
            logger.info(f"Inserted {len(text)} chars via pynput")
            self._status("Dictation inserted.")
            return True
        except Exception as e:
            logger.error(f"Text insertion failed via all paths: {e}")
            self._status("Could not insert text.")
            return False

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
