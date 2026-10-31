"""Phase 2: ClaudeOverlay.load_state repaints the notch from a swapped-in session's stored state.
Cost-free. Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_focus_render.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from app.ui.claude_overlay import ClaudeOverlay


class _Req:
    def __init__(self, tool, tool_input=None):
        self.tool, self.tool_input = tool, (tool_input or {})


def main():
    ov = ClaudeOverlay()
    ov.restore_panel()

    # Session A: working, with a feed.
    ov.load_state("Build Website", ["▸ build a website", "Writing index.html", "Running npm install"],
                  status="working", action="Running npm install")
    assert ov._transcript.text() == "Build Website", ov._transcript.text()
    feed = ov._details_feed.toPlainText()
    assert "Writing index.html" in feed and "Running npm install" in feed, feed
    assert "find my tax" not in feed.lower()

    # Swap focus to Session B: a different conversation, blocked on approval.
    ov.load_state("Find Tax Docs", ["▸ find my tax docs", "Searching your files"],
                  status="needs_you", approval_req=_Req("Bash", {"command": "ls ~/Documents"}))
    assert ov._transcript.text() == "Find Tax Docs", ov._transcript.text()
    feed_b = ov._details_feed.toPlainText()
    assert "Searching your files" in feed_b, feed_b
    assert "index.html" not in feed_b, "old session's feed leaked after focus-swap"
    assert ov._state == "needs_you", ov._state

    # Swap to a done session.
    ov.load_state("Build Website", ["▸ build a website", "Done"], status="done",
                  result="Created index.html and styles.css.", cost=0.42, session_id="sidA123")
    assert ov._transcript.text() == "Build Website"
    assert ov._state == "done", ov._state

    print("test_focus_render OK")


if __name__ == "__main__":
    main()
