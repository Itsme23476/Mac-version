"""REGRESSION (real macOS, not offscreen): the notch overlay must sit at the VERY TOP of the screen,
over the menu bar, so hovering/clicking the physical notch reaches it. setFloatingPanel: silently reset
the panel level to 3, AppKit then kept the window below the menu bar (y=33) while Qt believed it was at
y=0 — hovering the notch did nothing; only passing just under it flickered in/out.
Needs the real macOS platform; shows the (invisible) resting strip for a moment.
Run: venv311/bin/python ai_file_organizer/tests/test_notch_top_edge.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication([])
import AppKit  # noqa: E402
AppKit.NSApp.setActivationPolicy_(1)            # accessory: no Dock icon, never takes focus
from app.ui import claude_overlay as co, notch_panel  # noqa: E402


def _pump(n=20):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.02)


def _native_top(ns):
    primary_h = AppKit.NSScreen.screens()[0].frame().size.height
    return primary_h - (ns.frame().origin.y + ns.frame().size.height)


def main():
    ov = co.ClaudeOverlay()
    ov.rest_idle()
    _pump(40)
    ov._hover_poll.stop()                        # don't let the real cursor drive it during the test
    ns = notch_panel._ns_window(co.WIN_TITLE)
    assert ns.level() == notch_panel._PANEL_LEVEL, f"panel level reset to {ns.level()}"
    assert _native_top(ns) == 0, f"resting notch strip sits at y={_native_top(ns)}, below the menu bar"
    assert ov.frameGeometry().top() == 0
    ov._hover_in = True
    ov._show_hover_bar()
    _pump(10)
    assert _native_top(ns) == 0, f"hover bar at y={_native_top(ns)}"
    ov._hide_hover_bar()
    ov.open_full()
    _pump(10)
    assert _native_top(ns) == 0, f"open card at y={_native_top(ns)}"
    ov.hide()
    print("test_notch_top_edge OK  (level 1000, strip + hover bar + card all at y=0 over the notch)")


if __name__ == "__main__":
    main()
