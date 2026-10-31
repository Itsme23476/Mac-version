"""Shared macOS mechanics for VoiceOS-style notch panels.

These are the hard-won, fragile bits — a non-activating floating panel that drops under the
notch, sits over every Space and fullscreen app WITHOUT stealing focus, and keeps that style
across the per-state resizes that macOS otherwise uses to silently drop it (the "funk beep /
dead clicks" bug). They were proven in organize_overlay.py (itself lifted from
quick_search_overlay.py); factored here so a second panel (the Assistant notch) doesn't become
a fourth copy that drifts.

Pure helpers keyed by window title — no widget/app imports, no-op off macOS, every call wrapped
so a panel still constructs headless (QT_QPA_PLATFORM=offscreen) and off darwin.

ponytail: organize_overlay still has its own inline copy; adopt these there in a follow-up
(guarded by its self-check) to collapse to one.

Headless self-check:
    cd /Users/damianosmalliaros/Developer/Mac_app && \
      QT_QPA_PLATFORM=offscreen venv311/bin/python \
      ai_file_organizer/app/ui/notch_panel.py
"""
import logging
import sys

logger = logging.getLogger(__name__)

_NONACTIVATING_PANEL = 1 << 7   # NSWindowStyleMaskNonactivatingPanel (128)
_PANEL_LEVEL = 1000             # NSScreenSaverWindowLevel — above normal windows


def _ns_window(win_title: str):
    """The NSWindow whose title matches, or None. (Qt sets the title via setWindowTitle.)"""
    try:
        from AppKit import NSApp
    except Exception:
        return None
    try:
        for w in NSApp.windows():
            try:
                if w.title() == win_title:
                    return w
            except Exception:
                continue
    except Exception:
        return None
    return None


def notch_screen_geo():
    """Qt geometry of the built-in display that has the notch (safeAreaInsets.top > 0), or None.
    Matched to its QScreen by logical size so Qt coordinates stay consistent for the slide."""
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSScreen
        from PySide6.QtGui import QGuiApplication
    except Exception:
        return None
    try:
        notch = None
        for s in NSScreen.screens():
            try:
                if s.safeAreaInsets().top > 0:
                    notch = s
                    break
            except Exception:
                continue
        if notch is None:
            return None
        fr = notch.frame()
        nw, nh = round(fr.size.width), round(fr.size.height)
        for qs in QGuiApplication.screens():
            g = qs.geometry()
            if abs(g.width() - nw) <= 2 and abs(g.height() - nh) <= 2:
                return g
    except Exception:
        return None
    return None


def notch_inset_top() -> int:
    """Height (logical pts) of the top chrome — the notch/menu-bar band — on the notch display,
    else the main display, else 0. The resting handle must hang JUST BELOW this; anchored at the
    screen's top edge it sits hidden behind the physical notch (looks off-centre / missing)."""
    if sys.platform != "darwin":
        return 0
    try:
        from AppKit import NSScreen
    except Exception:
        return 0
    try:
        notch = None
        for s in NSScreen.screens():
            try:
                if s.safeAreaInsets().top > 0:
                    notch = s
                    break
            except Exception:
                continue
        s = notch or NSScreen.mainScreen()
        if s is None:
            return 0
        fr, vf = s.frame(), s.visibleFrame()
        menubar = (fr.origin.y + fr.size.height) - (vf.origin.y + vf.size.height)   # top chrome
        try:
            menubar = max(menubar, s.safeAreaInsets().top)
        except Exception:
            pass
        return int(round(max(0, menubar)))
    except Exception:
        return 0


def screen_geo():
    """Where the panel should anchor: the notch display, else the cursor's screen, else primary."""
    geo = notch_screen_geo()
    if geo is not None:
        return geo
    try:
        from PySide6.QtGui import QGuiApplication, QCursor
    except Exception:
        return None
    screen = None
    try:
        screen = QGuiApplication.screenAt(QCursor.pos())
    except Exception:
        screen = None
    if screen is None:
        try:
            screen = QGuiApplication.primaryScreen()
        except Exception:
            screen = None
    return screen.geometry() if screen is not None else None


def configure_macos_panel(win_title: str) -> None:
    """Make the titled window a non-activating panel over all Spaces / fullscreen, no focus steal.

    ROOT CAUSE (verified on macOS 26 / Tahoe, in organize_overlay): a background accessory app
    summoned without a user click CANNOT become the key window, so every "make it key" attempt
    produced the funk beep + dead buttons. The fix is to NOT need key focus — a non-activating
    panel receives mouse clicks while another app stays frontmost. Mouse-only UI, so it never
    needs the keyboard (the one thing a non-key window can't get)."""
    if sys.platform != "darwin":
        return
    ns = _ns_window(win_title)
    if ns is None:
        return
    try:
        ns.setCollectionBehavior_((1 << 0) | (1 << 8))   # CanJoinAllSpaces | FullScreenAuxiliary
        ns.setLevel_(_PANEL_LEVEL)
        if hasattr(ns, "setHidesOnDeactivate_"):
            ns.setHidesOnDeactivate_(False)
        # Set the style mask ONCE (idempotent) — re-setting it each call forces window-frame
        # recomputes that, under rapid show/hide, raced the drop-shadow blur into a crash.
        if hasattr(ns, "setStyleMask_") and hasattr(ns, "styleMask"):
            cur = ns.styleMask()
            if not (cur & _NONACTIVATING_PANEL):
                ns.setStyleMask_(cur | _NONACTIVATING_PANEL)
        if hasattr(ns, "setFloatingPanel_"):
            ns.setFloatingPanel_(True)
        if hasattr(ns, "setBecomesKeyOnlyIfNeeded_"):
            ns.setBecomesKeyOnlyIfNeeded_(True)
        try:
            from .mac_spaces import move_window_to_active_space
            move_window_to_active_space(ns.windowNumber())
        except Exception:
            pass
        ns.orderFrontRegardless()
        logger.info(f"[notch_panel] non-activating panel ready, style={ns.styleMask()}")
    except Exception as e:
        logger.warning(f"[notch_panel] panel config failed: {e}")
        try:
            ns.orderFrontRegardless()
        except Exception:
            pass


def reassert_panel_style(win_title: str) -> None:
    """Re-apply the non-activating bit + level. Qt re-syncs the native styleMask when geometry
    constraints change (our per-state resizes), which drops the panel bit and brings back the
    beep / dead clicks. Idempotent — only acts when the bit was actually lost."""
    if sys.platform != "darwin":
        return
    ns = _ns_window(win_title)
    if ns is None:
        return
    try:
        if hasattr(ns, "styleMask") and hasattr(ns, "setStyleMask_"):
            cur = ns.styleMask()
            if not (cur & _NONACTIVATING_PANEL):
                ns.setStyleMask_(cur | _NONACTIVATING_PANEL)
                if hasattr(ns, "setBecomesKeyOnlyIfNeeded_"):
                    ns.setBecomesKeyOnlyIfNeeded_(True)
                logger.info(f"[notch_panel] re-asserted non-activating style -> {ns.styleMask()}")
        try:
            ns.setLevel_(_PANEL_LEVEL)
        except Exception:
            pass
    except Exception:
        pass


# --- headless self-check -------------------------------------------------------
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)
    w = QWidget()
    w.setWindowTitle("Filect NotchPanel Selftest")
    w.show()

    # Off-darwin (or headless) these are all no-ops that must not raise.
    configure_macos_panel("Filect NotchPanel Selftest")
    reassert_panel_style("Filect NotchPanel Selftest")
    configure_macos_panel("does-not-exist")      # missing window -> no-op, no raise
    reassert_panel_style("does-not-exist")
    g = screen_geo()                              # None headless, a QRect with a real display
    ng = notch_screen_geo()                       # None unless a notch display is present
    assert g is None or hasattr(g, "width"), "screen_geo returned something odd"
    assert ng is None or hasattr(ng, "width"), "notch_screen_geo returned something odd"
    print("notch_panel self-check OK")
