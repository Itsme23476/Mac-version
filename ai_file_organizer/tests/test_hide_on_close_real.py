"""Real macOS: the red button (NSWindow.performClose:) only HIDES Filect's main window and the app keeps
running; the menu-bar icon's 'Open Filect' brings it back; its 'Quit Filect' really quits.
Uses Filect's own FilectApplication, MainWindow.closeEvent and the tray menu.
Needs the real macOS platform (not offscreen); a small test window flashes for ~2s.
Run: venv311/bin/python ai_file_organizer/tests/test_hide_on_close_real.py"""
import logging, sys, time
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
logging.basicConfig(level=logging.INFO, format="%(message)s")
import main as filect_main
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow
app = filect_main.FilectApplication([])
app.setQuitOnLastWindowClosed(False)                     # as main() now does
import AppKit
from app.ui.main_window import MainWindow
from app.ui import assistant_page as ap

class _Win(MainWindow):                 # the REAL closeEvent; skip the heavy app setup + first-run onboarding
    def __init__(self): QMainWindow.__init__(self)
    def showEvent(self, e): QMainWindow.showEvent(self, e)
win = _Win()
win.setWindowTitle("Filect close test"); win.resize(500, 300)
app.set_main_window(win)
tray = ap._get_or_create_tray()
acts = {a.text(): a for a in tray.contextMenu().actions()}
steps = []

def ns():
    return next(w for w in AppKit.NSApp.windows() if w.title() == "Filect close test")

def s1():
    win.show(); steps.append(f"shown={win.isVisible()}")
    QTimer.singleShot(400, s2)
def s2():
    ns().performClose_(None)                              # = clicking the red close button
    QTimer.singleShot(600, s3)
def s3():
    steps.append(f"after red button: visible={win.isVisible()} app_running=True quitting={app._quitting}")
    acts["Open Filect"].trigger()                         # menu-bar icon → Open Filect
    QTimer.singleShot(800, s4)
def s4():
    steps.append(f"after 'Open Filect': visible={win.isVisible()}")
    acts["Quit Filect"].trigger()                         # menu-bar icon → Quit Filect
    QTimer.singleShot(4000, lambda: (steps.append("QUIT DID NOT HAPPEN"), app.exit(7)))

QTimer.singleShot(300, s1)
QTimer.singleShot(15000, lambda: (steps.append('TIMEOUT'), app.exit(9)))
code = app.exec()
print("STEPS:", steps)
print("menu:", list(acts), "exit code:", code, "quitting:", app._quitting)
assert steps[0] == "shown=True"
assert "visible=False" in steps[1] and "quitting=False" in steps[1], steps[1]
assert steps[2] == "after 'Open Filect': visible=True", steps[2]
assert code == 0 and app._quitting, "Quit Filect must really quit"
print("test_hide_on_close_real OK")
