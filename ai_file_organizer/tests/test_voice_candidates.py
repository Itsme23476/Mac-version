"""Resume-by-name: _build_voice_candidates must (a) filter the router's own junk sessions, and
(b) surface an OLD named conversation (Hyper3D) that's far outside the 20 newest — the exact miss we
saw. Cost-free (fake list_sessions). Run offscreen."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from types import SimpleNamespace as NS
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import app.core.agent_sessions as asess
from app.ui import assistant_page as ap
ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None


def mk(i, title, preview="", mtime=0.0):
    return NS(id=i, title=title, preview=preview, mtime=mtime, cwd="/x", project="p", when="now")


class _MW:
    pass


def main():
    fake = [mk("j1", "Current conversation: (none) Other conversations: 0. Filect", "Spoken command: 'x'", 300),
            mk("j2", "Current conversation: Build One Page Website Other conv", "Spoken command: 'y'", 299),
            mk("stub", "This session is being continued from a previous conversation", "", 298)]
    fake += [mk(f"r{n}", f"Recent Task {n}", f"do recent thing {n}", 200 - n) for n in range(15)]
    fake.append(mk("hd", "Set up Hyper3D MCP in this client (Codex or Claude Code)",
                   "set up hyper3d mcp server", 50))        # OLD — far outside the 20 newest
    fake.sort(key=lambda s: s.mtime, reverse=True)
    asess.list_sessions = lambda limit=500: fake[:limit]

    page = ap.AssistantPage(_MW())
    page._sessions = {}

    cands = page._build_voice_candidates("use my hyper3d mcp conversation and spin it up")
    titles = [c["title"] for c in cands]

    assert not any(t.lower().startswith("current conversation:") for t in titles), f"router junk leaked: {titles}"
    assert not any("continued from a previous" in t.lower() for t in titles), "stub leaked"
    assert any("Hyper3D" in t for t in titles), f"Hyper3D not reachable by name! got: {titles}"
    assert len(titles) == len(set(titles)), "duplicate candidates"
    hd = next(c for c in cands if "Hyper3D" in c["title"])
    assert hd["id"] == "hd", hd
    print("test_voice_candidates OK — junk filtered, old Hyper3D reachable by name")


if __name__ == "__main__":
    main()
