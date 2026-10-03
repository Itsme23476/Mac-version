"""
Voice-driven "organize" — the controller that sequences the feature.

Wiring only: it owns the OrganizeOverlay, drives a VoiceRecorder for the spoken
instruction (and for refinements), and delegates the actual AI planning/applying to
main_window.organize_page (the OrganizePage engine). All heavy work happens off-thread
in those two collaborators; this class just moves messages between them and the overlay.

Two fixed contracts it codes against: OrganizeOverlay (app.ui.organize_overlay) and the
voice_* API/signals on OrganizePage. See the module docstrings there.
"""
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QFileDialog

logger = logging.getLogger(__name__)


class _FolderResolveWorker(QThread):
    """Ask the AI which real folder the spoken instruction means, OFF the GUI thread
    (the resolver does a network call). Emits the chosen path, or "" if none matched."""
    done = Signal(str)

    def __init__(self, instruction, candidates):
        super().__init__()
        self._instruction = instruction
        self._candidates = candidates

    def run(self):
        path = None
        try:
            from app.core.ai_organizer import resolve_folder_with_ai
            path = resolve_folder_with_ai(self._instruction, self._candidates)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[voice organize] folder resolve worker failed: {e}")
        self.done.emit(path or "")

# Real overlay import kept at module top. It's authored in parallel; if it isn't
# importable yet (or when this file is run standalone for the self-check) fall back
# to None and let the self-check inject a stub.
try:
    from app.ui.organize_overlay import OrganizeOverlay
except Exception:  # pragma: no cover - exercised only before the overlay lands
    OrganizeOverlay = None


class VoiceOrganizeController(QObject):
    """Sequences the voice "organize" flow: listen -> plan -> (refine) -> apply."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.overlay = OrganizeOverlay()

        self._recorder = None       # the live VoiceRecorder (kept referenced so it isn't GC'd)
        self._graveyard = None       # the just-finished recorder, held until GC-safe
        self._refining = False
        self._plan_active = False    # True while the panel shows a plan (enables refine-on-respeak)
        self._last_instruction = ''
        self._folder = ''
        self._resolve_worker = None  # live _FolderResolveWorker, kept referenced while it runs
        self._req_id = 0             # bumped per utterance / on dismiss; stale resolves are dropped

        # Overlay -> controller. Overlay lives on this (GUI) thread, so auto-connect is fine.
        self.overlay.refine_requested.connect(self._on_refine_toggle)
        self.overlay.organize_clicked.connect(self._on_organize_clicked)
        self.overlay.change_folder_requested.connect(self._on_change_folder)
        self.overlay.dismissed.connect(self._cleanup)

        # Engine -> controller. These fire off-thread, so queue them onto the GUI thread.
        page = main_window.organize_page
        page.voice_plan_ready.connect(self._on_plan_ready, Qt.QueuedConnection)
        page.voice_plan_error.connect(self._on_plan_error, Qt.QueuedConnection)
        page.voice_apply_done.connect(self._on_apply_done, Qt.QueuedConnection)
        page.voice_apply_error.connect(self._on_apply_error, Qt.QueuedConnection)
        page.voice_status.connect(self._on_status, Qt.QueuedConnection)

        self._init_debug_inject()

    def _init_debug_inject(self):
        """DEV-ONLY: poll a temp file so spoken instructions can be injected for testing
        WITHOUT the mic. Gated to the dev bundle ('Filect Dev.app') — never active in the
        shipping build. Write a line to /tmp/filect_voice_inject.txt and it runs through the
        exact same pipeline as a real utterance (folder resolution -> plan -> panel)."""
        import sys
        paths = (sys.executable or "") + "|" + (sys.argv[0] if sys.argv else "")
        if "Filect Dev.app" not in paths:
            return
        from PySide6.QtCore import QTimer
        self._inject_path = "/tmp/filect_voice_inject.txt"
        self._inject_mtime = 0.0
        self._inject_timer = QTimer(self)
        self._inject_timer.setInterval(600)
        self._inject_timer.timeout.connect(self._poll_inject)
        self._inject_timer.start()
        logger.info("[voice organize] DEV inject hook active -> /tmp/filect_voice_inject.txt")

    def _poll_inject(self):
        import os
        try:
            m = os.path.getmtime(self._inject_path)
        except Exception:
            return
        if m == self._inject_mtime:
            return
        self._inject_mtime = m
        try:
            with open(self._inject_path, "r") as f:
                text = f.read().strip()
        except Exception:
            return
        if not text:
            return
        if text == "__reset__":
            logger.info("[voice organize] INJECT reset (dismiss + clear active plan)")
            try:
                self.overlay.dismiss()
            except Exception:
                pass
            self._cleanup()
            return
        if text == "__apply__":
            logger.info("[voice organize] INJECT apply (= click Organize)")
            self._on_organize_clicked()
            return
        if text == "__demoplan__":
            # DEV: show a full plan without the AI (zero credits) to test the panel UI.
            logger.info("[voice organize] INJECT demo plan")
            self.overlay.present()
            self.overlay.show_plan(
                "Move 13 files into 7 folders",
                {"images": ["a.png", "b.jpg"], "documents": ["c.pdf", "notes.md"],
                 "other": ["misc.txt"]},
                str(Path.home() / "Desktop" / "test"), 13, 7)
            return
        if text == "__demothink__":
            logger.info("[voice organize] INJECT demo thinking")
            self.overlay.present()
            self.overlay.show_thinking("Analyzing your files…")
            return
        if text == "__demoerror__":
            # DEV: show the couldn't-resolve error (with the folder picker) without the AI.
            logger.info("[voice organize] INJECT demo error")
            self.overlay.present()
            self.overlay.show_error(
                "Couldn't tell which folder you meant. Say it again with the name (Fn+⌥), "
                "or choose it below.", allow_pick_folder=True)
            return
        if text.startswith("__folder__"):
            path = text[len("__folder__"):].strip()
            logger.info(f"[voice organize] INJECT change folder -> {path!r}")
            if path:
                self._folder = path
                if self._last_instruction:
                    self.overlay.show_thinking()
                    self.main_window.organize_page.voice_generate(self._folder, self._last_instruction)
            return
        logger.info(f"[voice organize] INJECT: {text!r}")
        self.start_from_transcript(text)

    # ------------------------------------------------------------------ hotkey
    def begin(self):
        """Hotkey PRESS (Fn+Option): open the overlay and start listening."""
        # imported lazily so the module loads without the app package / audio stack present
        from app.core.settings import settings
        target = getattr(settings, 'organize_voice_target', '') or ''
        if not target:
            dest = getattr(self.main_window.organize_page, 'destination_path', None)
            if dest:
                target = str(dest)
        if not target:
            target = str(Path.home() / 'Downloads')
        self._folder = target

        self.overlay.present()
        self.overlay.show_listening()
        self._start_recorder(self._on_instruction)

    def end_listening(self):
        """Hotkey RELEASE: stop the mic. Transcript arrives via the finished signal."""
        rec = self._recorder
        if rec is not None:
            rec.stop_recording()

    def start_from_transcript(self, text):
        """Entry when the dictation controller captured the spoken instruction itself
        (Fn+Option is decided at release now). Open the panel, let the AI pick the target
        folder from what was said, then build the plan. No recording here; refine-by-voice
        still uses our own recorder."""
        text = (text or '').strip()
        self.overlay.present()
        if not text:
            self.overlay.show_error("Didn't catch that — hold Fn+Option and say it again.")
            return
        logger.info(f"[voice organize] heard: {text!r}")
        self._last_instruction = text
        # The AI reads the whole sentence and picks ONE of the user's REAL folders. This
        # replaces keyword matching, which kept grabbing a reserved parent (the whole Desktop)
        # whenever the named subfolder didn't exist.
        self.overlay.show_thinking("Finding the folder…")
        self._req_id += 1
        rid = self._req_id
        worker = _FolderResolveWorker(text, self._folder_candidates())
        self._resolve_worker = worker  # keep referenced so the QThread isn't GC'd mid-run
        worker.done.connect(lambda path, t=text, r=rid: self._on_folder_resolved(path, t, r),
                            Qt.QueuedConnection)
        worker.start()

    def _on_folder_resolved(self, path, text, rid):
        if rid != self._req_id:
            # A newer utterance or a dismiss happened while this resolve was in flight.
            # Drop the stale result so it can't resurrect a plan the user moved on from.
            logger.info(f"[voice organize] ignoring stale folder resolve (#{rid} != #{self._req_id})")
            return
        path = (path or '').strip()
        if path:
            self._folder = path
            self._last_instruction = text
            logger.info(f"[voice organize] target folder = {path}")
            self.overlay.show_thinking()
            self.main_window.organize_page.voice_generate(self._folder, text)
            return
        if self._plan_active:
            # A plan is already up and the AI couldn't pin a folder from this utterance ->
            # treat it as an edit to the existing plan rather than re-resolving.
            logger.info("[voice organize] no folder matched; refining active plan")
            self.overlay.show_thinking("Updating the plan…")
            self.main_window.organize_page.voice_refine(text)
            return
        # Never fall back to a parent. Ask the user to name it, or pick it manually.
        logger.info("[voice organize] AI could not resolve a folder — asking the user")
        self.overlay.show_error(
            "Couldn't tell which folder you meant. Say it again with the name (Fn+⌥), "
            "or choose it below.",
            allow_pick_folder=True)

    def _folder_candidates(self):
        """Real folders the AI may choose from: the three top-level locations the user
        organizes in, plus their immediate subfolders. Bounded so the prompt stays small."""
        import os
        home = Path.home()
        out, seen = [], set()

        def add(p):
            s = str(p)
            if s not in seen:
                seen.add(s)
                out.append(s)

        for parent in (home / "Desktop", home / "Downloads", home / "Documents"):
            try:
                if not parent.is_dir():
                    continue
            except Exception:
                continue
            add(parent)
            try:
                entries = sorted(os.listdir(parent))
            except Exception:
                continue
            for name in entries:
                if name.startswith('.'):
                    continue
                sub = parent / name
                try:
                    if sub.is_dir():
                        add(sub)
                except Exception:
                    continue
                if len(out) >= 400:
                    return out
        return out

    def _on_status(self, msg):
        """Transient status from the engine (e.g. 'Indexing your files…')."""
        logger.info(f"[voice organize] status: {msg}")
        self.overlay.show_thinking(msg)

    # ------------------------------------------------------------- recorder i/o
    def _start_recorder(self, on_text):
        from app.core.settings import settings
        from app.core.transcription import VoiceRecorder
        rec = VoiceRecorder(terms=settings.dictation_custom_terms,
                            language=(settings.dictation_language or None))
        self._recorder = rec
        rec.level.connect(self.overlay.set_level, Qt.QueuedConnection)
        rec.finished.connect(lambda text, r=rec: self._recorder_done(r, on_text, text),
                             Qt.QueuedConnection)
        rec.error.connect(lambda msg, r=rec: self._recorder_failed(r, msg),
                          Qt.QueuedConnection)
        rec.start()

    def _recorder_done(self, rec, on_text, text):
        self._release_recorder(rec)
        on_text(text)

    def _recorder_failed(self, rec, msg):
        self._release_recorder(rec)
        self._on_rec_error(msg)

    def _release_recorder(self, rec):
        """Disconnect a finished recorder and keep it referenced one slot longer.

        The custom finished/error signals fire from inside run() just before it returns,
        so nulling the only reference here could free the QThread mid-run. Parking it in
        _graveyard keeps it alive until the next recording replaces it."""
        try:
            rec.level.disconnect()
            rec.finished.disconnect()
            rec.error.disconnect()
        except Exception:
            pass
        if self._recorder is rec:
            self._recorder = None
        self._graveyard = rec

    # --------------------------------------------------------- initial instruction
    def _on_instruction(self, text):
        if not text.strip():
            self.overlay.show_error("Didn't catch that — try again")
            return
        self._last_instruction = text
        self.overlay.show_thinking()
        self.main_window.organize_page.voice_generate(self._folder, text)

    # ------------------------------------------------------------------- refine
    def _on_refine_toggle(self):
        if not self._refining:
            self._refining = True
            self.overlay.set_refining(True)
            self.overlay.show_listening()
            self._start_recorder(self._on_refine_text)
        else:
            self._refining = False
            self.overlay.set_refining(False)
            self.overlay.show_thinking("Transcribing…")   # immediate feedback on Stop
            rec = self._recorder
            if rec is not None:
                rec.stop_recording()

    def _on_refine_text(self, text):
        self._refining = False
        self.overlay.set_refining(False)
        if text.strip():
            self.overlay.show_thinking()
            self.main_window.organize_page.voice_refine(text)
        else:
            # Caught nothing — don't strand the panel on the listening/thinking page.
            logger.info("[voice organize] refine captured no speech; returning to plan")
            self.overlay.show_plan_page()

    # ----------------------------------------------------------- engine results
    def _on_plan_ready(self, payload):
        self._plan_active = True
        self.overlay.show_plan(payload['summary'], payload['folders'],
                               payload['target_folder'], payload['file_count'],
                               payload['folder_count'])

    def _on_plan_error(self, msg):
        self._plan_active = False
        logger.info(f"[voice organize] plan error: {msg}")
        self.overlay.show_error(msg)

    def _on_organize_clicked(self):
        self.overlay.show_applying()
        self.main_window.organize_page.voice_apply()

    def _on_apply_done(self, hint):
        self._plan_active = False
        logger.info(f"[voice organize] apply done: {hint}")
        self.overlay.show_done(hint)

    def _on_apply_error(self, msg):
        logger.info(f"[voice organize] apply error: {msg}")
        self.overlay.show_error(msg)

    # ------------------------------------------------------------- change folder
    def _on_change_folder(self):
        # The native folder picker can't take keyboard/focus while we're a background,
        # non-activating accessory app (macOS 26) — it plays the funk beep and freezes. Briefly
        # become a Regular, active app (the same trick main.py uses for the login dialog) so the
        # Open panel is usable, then restore the menu-bar accessory mode. Parent is None (not the
        # non-activating overlay) so the panel isn't tied to a window that can't be key.
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        flipped = False
        try:
            if app is not None and hasattr(app, "set_normal_focus_mode"):
                app.set_normal_focus_mode(True)
                flipped = True
            try:
                from AppKit import NSApp
                NSApp.activate()
            except Exception:
                pass
            chosen = QFileDialog.getExistingDirectory(
                None, "Choose folder to organize", self._folder or str(Path.home()))
        finally:
            if flipped:
                try:
                    app.set_normal_focus_mode(False)
                except Exception:
                    pass
        if chosen:
            self._folder = chosen
            if self._last_instruction:
                self.overlay.show_thinking()
                self.main_window.organize_page.voice_generate(self._folder, self._last_instruction)

    # -------------------------------------------------------------------- teardown
    def _on_rec_error(self, msg):
        self.overlay.show_error(msg)

    def _cleanup(self):
        rec = self._recorder
        if rec is not None:
            rec.stop_recording()
        self._refining = False
        self._plan_active = False
        self._req_id += 1  # invalidate any in-flight folder resolve so it can't re-open a plan


if __name__ == "__main__":
    # Headless self-check (QT_QPA_PLATFORM=offscreen). Uses a fake main_window + the real
    # OrganizeOverlay if importable, else a minimal stub with the same signals/methods.
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QObject, Signal
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    import voice_organize_controller as m

    class FakeOrganizePage(QObject):
        voice_plan_ready = Signal(dict)
        voice_plan_error = Signal(str)
        voice_apply_done = Signal(str)
        voice_apply_error = Signal(str)
        voice_status = Signal(str)

        def __init__(self):
            super().__init__()
            self.destination_path = None
            self.apply_called = False

        def voice_generate(self, folder, instruction):
            pass

        def voice_refine(self, feedback):
            pass

        def voice_apply(self):
            self.apply_called = True

        def has_plan(self):
            return False

    class FakeMainWindow:
        def __init__(self):
            self.organize_page = FakeOrganizePage()

    if m.OrganizeOverlay is None:
        class StubOverlay(QObject):
            refine_requested = Signal()
            organize_clicked = Signal()
            change_folder_requested = Signal()
            dismissed = Signal()

            def __init__(self):
                super().__init__()
                self.plan_shown = False
                self.applying = False
                self.last_error = None

            def present(self): pass
            def dismiss(self): pass
            def set_level(self, v): pass
            def set_refining(self, v): pass
            def show_listening(self): pass
            def show_thinking(self, msg=""): pass
            def show_applying(self): self.applying = True
            def show_done(self, revert_hint): pass
            def show_error(self, msg): self.last_error = msg
            def show_plan(self, summary, folders, target_folder, file_count, folder_count):
                self.plan_shown = True

        m.OrganizeOverlay = StubOverlay

    mw = FakeMainWindow()
    c = m.VoiceOrganizeController(mw)
    assert c.overlay is not None, "overlay not wired"

    # Plan ready drives show_plan without throwing (queued -> needs an event-loop tick).
    sample = {'summary': 'Group by type', 'folders': {'Docs': ['a.pdf']},
              'target_folder': '/tmp', 'file_count': 1, 'folder_count': 1}
    mw.organize_page.voice_plan_ready.emit(sample)
    QCoreApplication.processEvents()
    assert getattr(c.overlay, 'plan_shown', True), "show_plan was not driven"

    # Organize click routes to the engine's voice_apply.
    c.overlay.organize_clicked.emit()
    QCoreApplication.processEvents()
    assert mw.organize_page.apply_called, "organize_clicked did not call voice_apply"

    print("voice_organize_controller self-check OK")
