"""Claude Code missing or signed out is a SETUP step, not a task failure — and never a fake "Done".
Signed out = the REAL Claude Code with an empty throwaway profile (CLAUDE_CONFIG_DIR), so your own
login is never touched. The card shows plain words + a one-click "Set up / Sign in" that opens Terminal
with Anthropic's official installer (captured here, not run).
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_claude_setup.py"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication([])
from app.core import agent_runner as ar  # noqa: E402
from app.ui import assistant_page as ap  # noqa: E402
ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None
ap.AssistantPage._load_recent = lambda self: None


def _run(runner):
    return asyncio.run(runner.run("say hi", max_turns=1))


def test_signed_out_is_a_setup_step_not_an_answer():
    done = []
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()      # a brand-new, signed-out profile
    try:
        r = ar.AgentRunner(tempfile.mkdtemp(), on_step=lambda s: None, on_approval=None,
                           on_result=lambda *a: done.append(a))
        try:
            _run(r)
            raise AssertionError("signed out must not finish like a normal task")
        except ar.AgentSetupError as e:
            assert e.kind == "signin" and str(e) == ar.SETUP_SIGNIN
        assert done == [], "no fake 'Done: Not logged in' card"
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old
    print("signed out (real Claude Code, empty profile) → setup step 'signin', no Done card  OK")


def test_missing_claude_code_is_a_setup_step():
    from claude_agent_sdk import CLINotFoundError
    from claude_agent_sdk._internal.transport import subprocess_cli as sc
    real = sc.SubprocessCLITransport._find_cli

    def nowhere(self):                                        # like the packaged app on a Mac without it
        raise CLINotFoundError("Claude Code not found. Install with: ...")
    sc.SubprocessCLITransport._find_cli = nowhere
    try:
        r = ar.AgentRunner(tempfile.mkdtemp(), on_step=lambda s: None, on_approval=None,
                           on_result=lambda *a: None)
        try:
            _run(r)
            raise AssertionError("missing Claude Code must raise")
        except ar.AgentSetupError as e:
            assert e.kind == "install" and str(e) == ar.SETUP_INSTALL
    finally:
        sc.SubprocessCLITransport._find_cli = real
    print("no Claude Code on the Mac → setup step 'install' (not the raw 'not found at' text)  OK")


class _MW:
    pass


def test_card_shows_the_fix():
    opened = []
    import subprocess
    orig = subprocess.Popen
    subprocess.Popen = lambda args, **k: opened.append(args)    # capture: don't really open Terminal
    try:
        page = ap.AssistantPage(_MW())
        sess = ap.AgentSession("s1", tempfile.mkdtemp(), "build me a site", "Build Site", True, "opus", "high")
        page._sessions = {"s1": sess}
        page._focused_key = page._last_sent_key = "s1"
        ov = page._ensure_notch()
        page._notch("present")
        page._on_error("s1", ar.SETUP_INSTALL)
        assert ov._status_label.text() == "Setup needed" and ov._setup_btn.isVisibleTo(ov)
        assert "Set up Claude Code" in ov._setup_btn.text() and not ov._primary_btn.isVisibleTo(ov)
        assert ov._error_label.text() == ar.SETUP_INSTALL
        ov._setup_btn.click()                                  # the real button → page → Terminal
        assert opened and opened[-1][:3] == ["open", "-a", "Terminal"], opened
        script = Path(opened[-1][3]).read_text()
        assert "curl -fsSL https://claude.ai/install.sh | bash" in script and script.rstrip().endswith("claude")
        page._on_error("s1", ar.SETUP_SIGNIN)                  # signed out: sign-in only, no installer
        assert "Sign in to Claude Code" in ov._setup_btn.text()
        ov._setup_btn.click()
        script = Path(opened[-1][3]).read_text()
        assert "install.sh" not in script and script.rstrip().endswith("claude")
        page._on_error("s1", "Something broke in the task")   # a REAL failure stays a red Error
        assert ov._status_label.text() == "Error" and not ov._setup_btn.isVisibleTo(ov)
        assert ov._primary_btn.isVisibleTo(ov) and "Open in Claude" in ov._primary_btn.text()
    finally:
        subprocess.Popen = orig
    print("card: plain words + 'Set up / Sign in to Claude Code' → Terminal with the official setup; "
          "real errors unchanged  OK")


if __name__ == "__main__":
    test_signed_out_is_a_setup_step_not_an_answer()
    test_missing_claude_code_is_a_setup_step()
    test_card_shows_the_fix()
    print("test_claude_setup OK")
