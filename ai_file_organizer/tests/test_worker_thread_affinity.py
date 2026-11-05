"""REGRESSION for the hard crash: worker signals must deliver on the MAIN thread. Drives the real
run_task -> AgentWorker(QThread) -> signal -> handler path with a fake runner (no real `claude`), and
asserts _on_step/_on_result run on the main thread (not the worker thread). If they ran on the worker
thread, the GUI/CGS calls inside them would crash the app — exactly what we saw.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_worker_thread_affinity.py
"""
import os, sys, time, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import app.core.agent_runner as ar
from app.ui import assistant_page as ap
ap._play_chime = lambda ok=True: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None   # don't spawn notch/rail during the test


class _FakeRunner:
    """Stands in for AgentRunner: emits one text step + a result, no subprocess."""
    def __init__(self, cwd, on_step, on_approval, on_result):
        self.on_step, self.on_result = on_step, on_result

    async def run(self, prompt, resume=None, max_turns=40, bypass=False, model=None, effort=None, images=None):
        self.on_step(ar.Step("text", text="hello from the worker"))
        self.on_result("sidFAKE", 0.01)
        return "sidFAKE"

    async def interrupt(self):
        pass


class _MW:
    pass


# Subclass override is still a genuine bound method of the QObject → Qt queues it to the main thread,
# exactly like the real connection. (Replacing the attr with a plain closure would NOT — that's the
# lambda bug — so we must override, not monkeypatch.)
class _TestPage(ap.AssistantPage):
    tids = {}

    def _on_step(self, key, step):
        _TestPage.tids["step"] = threading.get_ident()
        return super()._on_step(key, step)

    def _on_result(self, key, sid, cost):
        _TestPage.tids["result"] = threading.get_ident()
        return super()._on_result(key, sid, cost)


def main():
    ar.AgentRunner = _FakeRunner                 # _go does a local import of this symbol
    main_tid = threading.get_ident()
    page = _TestPage(_MW())
    page._primary = "claude"                  # the fake runner is Claude's (whatever your saved primary is)
    tids = _TestPage.tids

    page.run_task("build a tiny thing")
    assert len(page._sessions) == 1
    key = next(iter(page._sessions))

    deadline = time.time() + 8
    while time.time() < deadline and page._sessions[key].status != "done":
        app.processEvents()
        time.sleep(0.02)

    sess = page._sessions[key]
    assert "step" in tids, "step handler never ran"
    assert tids["step"] == main_tid, f"step ran on worker thread {tids['step']} != main {main_tid}"
    assert tids.get("result") == main_tid, f"result ran off main thread: {tids.get('result')}"
    assert sess.status == "done", f"status={sess.status}"
    assert sess.session_id == "sidFAKE", sess.session_id
    assert "hello from the worker" in " ".join(sess.feed_lines), sess.feed_lines
    print("test_worker_thread_affinity OK  (handlers ran on the main thread)")


if __name__ == "__main__":
    main()
