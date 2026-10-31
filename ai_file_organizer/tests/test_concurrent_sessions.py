"""Phase 1 engine: concurrent AgentSessions must not cross-talk. Each worker's signals (keyed) update
ONLY its own session; focus-swap repaints the notch from the focused session's stored state. Cost-free
— no real workers, no network, no files moved. Run: venv311/bin/python ai_file_organizer/tests/test_concurrent_sessions.py
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from app.ui import assistant_page as ap
ap._play_chime = lambda ok=True: None          # no sound / no OS notification during the test
ap._notify = lambda *a, **k: None


class _Step:
    def __init__(self, kind, tool="", text="", detail=""):
        self.kind, self.tool, self.text, self.detail = kind, tool, text, detail


class _Req:
    def __init__(self, tool, tool_input=None):
        self.tool, self.tool_input = tool, (tool_input or {})


class _MW:                                      # the page only uses mw in _open_task
    pass


def main():
    page = ap.AssistantPage(_MW())
    notch_calls = []
    page._notch = lambda method, *a: notch_calls.append((method, a))   # capture, no real overlay

    # Two concurrent conversations, no real threads.
    a = ap.AgentSession("s1", "/tmp/a", "build a website", "Build Website", False, "opus", "high")
    b = ap.AgentSession("s2", "/tmp/b", "find my tax docs", "Find Tax Docs", False, "opus", "high")
    a.feed_lines = ["▸ build a website"]
    b.feed_lines = ["▸ find my tax docs"]
    page._sessions = {"s1": a, "s2": b}
    page._focused_key = "s1"

    # --- steps route to the right session, no cross-talk ---
    page._on_step("s1", _Step("tool", tool="Write", detail="index.html"))
    page._on_step("s2", _Step("tool", tool="Bash", detail="npm install"))
    assert a.last_action == "Writing index.html", a.last_action
    assert b.last_action == "Running npm install", b.last_action
    assert a.feed_lines[-1] == "Writing index.html"
    assert b.feed_lines[-1] == "Running npm install"
    assert "Writing index.html" not in b.feed_lines, "cross-talk: s1's action leaked into s2"
    assert "Running npm install" not in a.feed_lines, "cross-talk: s2's action leaked into s1"

    # a text step accumulates into result_text of its own session only
    page._on_step("s2", _Step("text", text="Found 3 PDFs."))
    assert b.result_text == "Found 3 PDFs." and a.result_text == "", (a.result_text, b.result_text)

    # --- approval gates per session ---
    page._on_approval("s2", _Req("Bash", {"command": "rm x"}))
    assert b.status == "needs_you" and b.approval_req is not None
    assert a.status == "working" and a.approval_req is None, "s2's approval must not touch s1"

    # _decide answers the FOCUSED session (s1), which has no gate → no-op; s2 still blocked
    page._decide(True)
    assert b.status == "needs_you" and b.approval_req is not None, "wrong session answered"

    # focus s2, then approve → resolves s2's gate (worker is None, guarded)
    page._focus("s2")
    assert page._focused_key == "s2"
    assert any(m == "load_state" and args[0] == "Find Tax Docs" for m, args in notch_calls), \
        "focus-swap must repaint the notch from s2's stored state"
    page._decide(True)
    assert b.approval_req is None and b.status == "working"

    # --- results / errors / lookup / teardown ---
    page._on_result("s1", "sidA123", 0.42)
    assert a.session_id == "sidA123" and a.status == "done" and a.cost == 0.42
    assert b.status == "working", "s1 finishing must not change s2"
    assert page._session_by_claude_id("sidA123") is a

    page._on_error("s2", "boom")
    assert b.status == "error" and b.result_text == "boom"

    page._on_done("s1")                         # thread is None → just a clean state transition
    assert a.thread is None and a.status == "done"

    print("test_concurrent_sessions OK")


if __name__ == "__main__":
    main()
