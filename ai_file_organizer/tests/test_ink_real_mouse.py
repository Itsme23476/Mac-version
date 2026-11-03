"""REGRESSION for "I was drawing but saw nothing": the gesture holds ⌃, and Qt on macOS turns ⌃-click
into a RIGHT click — the ink layer only took the left button, so every stroke was dropped. This drives
Qt's real NSEvent→QMouseEvent translation (NSApp.sendEvent_, with fn+⌃ in the flags) on a real window,
then reads the window's pixels back from the window server to prove the red ink is ON SCREEN.
Needs the real macOS platform (NOT offscreen); shows a small window for ~0.5s.
Run: venv311/bin/python ai_file_organizer/tests/test_ink_real_mouse.py
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRect
app = QApplication.instance() or QApplication([])
import AppKit
import Quartz as Q
AppKit.NSApp.setActivationPolicy_(1)            # accessory: no Dock icon, never takes focus
from app.ui import screen_ink, notch_panel


def _pump(n=10):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.02)


def _red_pixels(win_number) -> int:
    img = Q.CGWindowListCreateImage(Q.CGRectNull, Q.kCGWindowListOptionIncludingWindow, win_number,
                                    Q.kCGWindowImageBoundsIgnoreFraming)
    w, h, bpr = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img), Q.CGImageGetBytesPerRow(img)
    raw = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img)))
    n = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            b, g, r, a = raw[y * bpr + 4 * x: y * bpr + 4 * x + 4]      # BGRA
            n += a > 200 and r > 200 and g < 110 and b < 110
    return n


def main():
    ink = screen_ink.ScreenInk()
    ink.setGeometry(QRect(10, 700, 160, 160))
    ink.show()
    ink._configure()
    ink.set_drawing(True)
    _pump()
    ns = notch_panel._ns_window(screen_ink.INK_TITLE)
    assert not ns.ignoresMouseEvents(), "ink layer is click-through while drawing"
    edge = _red_pixels(ns.windowNumber())                 # the red "you're drawing" edge alone

    def ev(t, x, y):
        flags = (1 << 23) | AppKit.NSEventModifierFlagControl          # fn + ⌃ held = the real gesture
        return AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            t, AppKit.NSMakePoint(x, y), flags, time.time(), ns.windowNumber(), None, 0, 1, 1.0)

    AppKit.NSApp.sendEvent_(ev(AppKit.NSEventTypeLeftMouseDown, 30, 30))
    for i in range(1, 11):
        AppKit.NSApp.sendEvent_(ev(AppKit.NSEventTypeLeftMouseDragged, 30 + 10 * i, 30 + 10 * i))
    AppKit.NSApp.sendEvent_(ev(AppKit.NSEventTypeLeftMouseUp, 130, 130))
    _pump()
    assert len(ink._strokes) == 1 and len(ink._strokes[0]) >= 10, f"⌃-drag dropped: {ink._strokes}"
    inked = _red_pixels(ns.windowNumber())
    assert inked > edge + 40, f"stroke not visible on screen: red px {edge} → {inked}"
    ink.end()
    print(f"test_ink_real_mouse OK  (⌃-drag drew a stroke; on-screen red px {edge} → {inked})")


if __name__ == "__main__":
    main()
