"""Idle notch hover (VoiceOS-style bar) — without changing what the notch does.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_notch_hover.py"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication([])
from app.ui import claude_overlay as co  # noqa: E402


def pump(ms):
    t = time.time()
    while time.time() - t < ms / 1000:
        app.processEvents()
        time.sleep(0.01)


def _overlay():
    ov = co.ClaudeOverlay()
    ov.rest_idle()
    pump(100)
    return ov


def _cursor(ov, inside):
    g = ov.frameGeometry()
    pt = QPoint(g.center().x(), g.top() + 5) if inside else QPoint(g.right() + 500, g.bottom() + 300)
    co.QCursor = NS(pos=lambda: pt)


def test_idle_hover_grows_and_tucks_back():
    real = co.QCursor
    try:
        ov = _overlay()
        strip = ov.width()
        _cursor(ov, True); pump(450)                       # rest on the notch → it grows
        assert ov._hover_bar_on and ov.width() == ov._hover_w() > strip
        assert ov._hover_bar.isVisible() and ov._minimized, "still the resting notch underneath"
        _cursor(ov, False); pump(500)                      # leave → tucks back
        assert not ov._hover_bar_on and ov.width() == strip and ov._handle.isVisible()

        _cursor(ov, True); pump(450)
        ov._hover_bar.field.setText("draft")               # typing → leaving must NOT lose it
        _cursor(ov, False); pump(500)
        assert ov._hover_bar_on, "mid-typing the bar stays"

        sent = []
        ov.submit_text.connect(sent.append)
        ov._hover_bar.field.setText("build me a site")
        ov._hover_bar.field.returnPressed.emit()           # Enter → same path as the card's type field
        assert sent == ["build me a site"] and not ov._hover_bar_on and ov.width() == strip
    finally:
        co.QCursor = real
    ov.hide()
    print("idle hover: grows on rest, tucks back on leave, keeps a draft, Enter submits  OK")


def test_click_on_bar_still_opens_full_card():
    real = co.QCursor
    try:
        ov = _overlay()
        _cursor(ov, True); pump(450)
        assert ov._hover_bar_on
        click = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton,
                            Qt.LeftButton, Qt.NoModifier)
        ov.mousePressEvent(click)                          # what an (ignored) press on the bar reaches
        pump(50)
        assert not ov._minimized and ov._pinned and not ov._hover_bar_on, "click opens the full card"
        assert ov.width() == co.WIDGET_W
    finally:
        co.QCursor = real
    ov.hide()
    print("click on the hover bar: opens the full card, as before  OK")


def test_live_task_keeps_status_pill():
    real = co.QCursor
    try:
        ov = _overlay()
        ov.set_state("working")                            # a task is running, notch resting
        _cursor(ov, True); pump(450)
        assert not ov._hover_bar_on, "no idle bar while a task runs"
        assert ov._handle._reveal_target == 1.0, "the existing status-pill hover still runs"
        ov.set_state("idle")
        _cursor(ov, False); pump(100)
        _cursor(ov, True); pump(450)
        assert ov._hover_bar_on
        ov.set_state("working")                            # a voice task starts while hovering
        assert not ov._hover_bar_on and ov.width() == ov._strip_w()
    finally:
        co.QCursor = real
    ov.hide()
    print("live task: status pill hover unchanged; a starting task closes the idle bar  OK")


if __name__ == "__main__":
    test_idle_hover_grows_and_tucks_back()
    test_click_on_bar_still_opens_full_card()
    test_live_task_keeps_status_pill()
    print("test_notch_hover OK")
