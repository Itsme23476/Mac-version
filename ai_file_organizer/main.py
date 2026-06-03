#!/usr/bin/env python3
"""
Filect - File Search Assistant v1.0
A privacy-first desktop application for intelligent file search and quick path autofill.
Instantly find and autofill file paths in any application using global hotkeys.
"""

import sys
import os
from pathlib import Path

# Handle PyInstaller bundled path vs running from source
if hasattr(sys, '_MEIPASS'):
    # Running as bundled exe - resources are in temp extraction folder
    project_root = Path(sys._MEIPASS)
    source_root = Path(sys._MEIPASS)
else:
    # Running from source
    project_root = Path(__file__).parent
    source_root = project_root

# Add the project root to Python path for consistent imports
sys.path.insert(0, str(project_root))

from urllib.parse import urlparse, parse_qs

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QIcon, QFileOpenEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Key for the single-instance lock (a named local socket).
SINGLE_INSTANCE_KEY = "filect-single-instance"
from app.ui.main_window import MainWindow
from app.ui.auth_dialog import AuthDialog
from app.core.logging_config import setup_logging
from app.core.supabase_client import supabase_auth, SUPABASE_AVAILABLE
from app.core.settings import settings


class FilectApplication(QApplication):
    """QApplication subclass that handles filect:// deep links on macOS."""

    def __init__(self, argv):
        super().__init__(argv)
        self._main_window = None
        self._auth_dialog = None
        # When macOS launches us via a filect:// URL while another instance is
        # already running, the URL arrives in THIS (new) instance as a Qt
        # FileOpen event. We stash it here so main() can forward it to the
        # running instance over the single-instance socket before exiting.
        self._pending_url = None

    def set_main_window(self, window):
        self._main_window = window

    def set_auth_dialog(self, dialog):
        self._auth_dialog = dialog

    def event(self, event):
        if event.type() == QEvent.FileOpen:
            url = event.url().toString()
            # Always stash — main() reads this to forward to an existing
            # instance if we're the second one launched.
            self._pending_url = url
            self._dispatch_url(url)
            return True
        return super().event(event)

    def _dispatch_url(self, url):
        if url.startswith('filect://verify'):
            self._handle_verify(url)
        elif url.startswith('filect://subscribe'):
            self._handle_subscribe()
        elif url.startswith('filect://auth-callback'):
            self._handle_auth_callback(url)
        elif url.startswith('filect://'):
            self._bring_to_front()

    def _handle_auth_callback(self, url):
        """Complete OAuth (Google) sign-in.

        Supabase redirects back to filect://auth-callback#access_token=...&refresh_token=...
        after the user completes Google sign-in in the browser. We extract the
        tokens from the URL fragment, install them as the active session, and
        let the auth dialog continue into the main window.
        """
        # Tokens come back in the URL FRAGMENT (#…), not query string — that's
        # Supabase's implicit OAuth flow. urlparse puts it in .fragment.
        parsed = urlparse(url)
        params = parse_qs(parsed.fragment) if parsed.fragment else parse_qs(parsed.query)
        access_token = params.get('access_token', [None])[0]
        refresh_token = params.get('refresh_token', [None])[0]

        if not access_token or not refresh_token:
            logger.error("filect://auth-callback received without tokens")
            return

        logger.info("OAuth callback: installing Google sign-in session")
        result = supabase_auth.restore_session(access_token, refresh_token)
        if not result.get('success'):
            logger.error(f"OAuth session install failed: {result.get('error')}")
            return

        # Persist so the user stays logged in next launch.
        tokens = supabase_auth.get_session_tokens()
        if tokens:
            settings.set_auth_tokens(
                tokens['access_token'],
                tokens['refresh_token'],
                supabase_auth.user_email or ''
            )

        # If the auth dialog is still up, run the same post-signin path as
        # email/password — it'll close the dialog and route to main window or
        # subscribe page based on subscription status.
        if self._auth_dialog and self._auth_dialog.isVisible():
            self._auth_dialog._check_subscription_silent()
        else:
            self._bring_to_front()

    def _handle_subscribe(self):
        """Bring the app forward and show the subscribe page (from a recovery email link)."""
        self._bring_to_front()
        # If the auth dialog is available, route it to the subscribe page so an
        # unsubscribed user lands directly on checkout.
        if self._auth_dialog:
            try:
                self.set_normal_focus_mode(True)
                self._auth_dialog.show()
                self._auth_dialog.raise_()
                self._auth_dialog.activateWindow()
                self._auth_dialog._show_subscribe_page()
            except Exception as e:
                logger.warning(f"Could not show subscribe page from deep link: {e}")

    def _handle_verify(self, url):
        """Verify email token from filect://verify?token_hash=...&type=signup deep link."""
        params = parse_qs(urlparse(url).query)
        token_hash = params.get('token_hash', [None])[0]

        if not token_hash:
            logger.warning("filect://verify received with no token_hash")
            return

        logger.info("Verifying email token from deep link")
        result = supabase_auth.verify_email_token(token_hash)

        if result.get('success'):
            # Persist the session so the user stays logged in
            tokens = supabase_auth.get_session_tokens()
            if tokens:
                settings.set_auth_tokens(
                    tokens['access_token'],
                    tokens['refresh_token'],
                    supabase_auth.user_email or ''
                )
            # If auth dialog is still open, run subscription check — it will
            # close the dialog and show the main window (or subscribe page)
            if self._auth_dialog and self._auth_dialog.isVisible():
                self._auth_dialog._check_subscription_silent()
            else:
                self._bring_to_front()
        else:
            logger.error(f"Email verification failed: {result.get('error')}")

    def set_normal_focus_mode(self, enabled: bool):
        """Toggle between Regular and Accessory activation policy on macOS.

        As an agent (Accessory) app, focusable windows like the login dialog
        don't release focus cleanly when the user clicks another app. Switching
        to Regular while such a window is shown restores normal focus behavior;
        we switch back to Accessory afterwards so the app stays a menu-bar agent.
        """
        if sys.platform != 'darwin':
            return
        try:
            from AppKit import (
                NSApp,
                NSApplicationActivationPolicyRegular,
                NSApplicationActivationPolicyAccessory,
            )
            NSApp.setActivationPolicy_(
                NSApplicationActivationPolicyRegular if enabled
                else NSApplicationActivationPolicyAccessory
            )
        except Exception as e:
            logger.warning(f"Could not change activation policy: {e}")

    def _bring_to_front(self):
        # Surface the main window if we have one, otherwise the auth dialog.
        # As an accessory (LSUIElement) app, activateIgnoringOtherApps_ is
        # throttled, so on a relaunch a plain show()/raise() often leaves the
        # window buried behind other apps — which made "close → reopen" look
        # like nothing happened (the window was there, just never brought
        # forward). Briefly flipping to Regular activation policy makes the
        # bring-to-front actually stick.
        win = self._main_window or self._auth_dialog
        if not win:
            return
        # If the auth dialog is the target it's running modally and main.py
        # already holds Regular policy for its whole lifetime — don't flip the
        # policy back underneath it (that re-introduces the 14.1.11 freeze).
        modal_dialog = self._main_window is None and self._auth_dialog is not None
        if sys.platform == 'darwin':
            self.set_normal_focus_mode(True)
        win.show()
        win.raise_()
        win.activateWindow()
        if sys.platform == 'darwin':
            try:
                from AppKit import NSApp
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
            if not modal_dialog:
                QTimer.singleShot(800, lambda: self.set_normal_focus_mode(False))


def acquire_single_instance(app) -> bool:
    """
    Ensure only one Filect window runs at a time.

    Returns True if this is the first/only instance (and sets up a listener to
    forward deep-link URLs from any future launches). Returns False if another
    instance is already running — in that case it forwards any pending filect://
    URL we received from macOS to the running instance, then the caller exits.
    """
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_KEY)
    if sock.waitForConnected(300):
        # Another instance is already running. macOS may have launched us with a
        # filect:// deep link (Apple Event). Give Qt a brief tick to surface that
        # FileOpen event into app._pending_url so we can forward it.
        for _ in range(20):
            app.processEvents()
            if getattr(app, '_pending_url', None):
                break
            import time as _t; _t.sleep(0.02)

        payload = (getattr(app, '_pending_url', None) or '').encode('utf-8')
        sock.write(payload)
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return False

    # First instance: clear any stale socket and start listening.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    def _on_new_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return
        # Wait briefly for the forwarded URL bytes; fall back to plain
        # bring-to-front if nothing arrives (manual second-launch).
        if conn.waitForReadyRead(500):
            data = bytes(conn.readAll()).decode('utf-8', errors='ignore').strip()
            if data:
                app._dispatch_url(data)
            else:
                app._bring_to_front()
        else:
            app._bring_to_front()
        conn.disconnectFromServer()

    server.newConnection.connect(_on_new_connection)
    # Keep a reference so the server isn't garbage-collected.
    app._single_instance_server = server
    return True


def check_existing_session():
    """
    Check if there's a valid stored session with active subscription.
    Returns True if user can skip login, False otherwise.
    """
    if not settings.has_stored_session():
        return False
    
    # Try to restore the session
    result = supabase_auth.restore_session(
        settings.auth_access_token,
        settings.auth_refresh_token
    )
    
    if not result.get('success'):
        # Session invalid, clear tokens
        settings.clear_auth_tokens()
        return False
    
    # Check subscription
    sub_result = supabase_auth.check_subscription()
    if sub_result.get('has_subscription'):
        return True
    
    return False


def main():
    """Main application entry point."""
    # Setup logging
    setup_logging()
    
    # On macOS, set the app as an agent (LSUIElement) BEFORE creating QApplication
    # This allows the app to appear over fullscreen apps like Spotlight/Alfred
    if sys.platform == 'darwin':
        try:
            from AppKit import NSApp, NSApplication, NSApplicationActivationPolicyAccessory
            # Initialize NSApplication if not already done
            NSApplication.sharedApplication()
            # Set as agent app - no dock icon, can appear over fullscreen
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            print("macOS: Set activation policy to ACCESSORY (agent app)")
        except ImportError:
            print("macOS: AppKit not available, running as regular app")
        except Exception as e:
            print(f"macOS: Error setting activation policy: {e}")
    
    # Create Qt application (FilectApplication handles filect:// deep links on macOS)
    app = FilectApplication(sys.argv)
    app.setApplicationName("Filect")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("Filect")

    # Single-instance guard: if Filect is already running, bring that window to
    # the front and exit instead of opening a second window.
    if not acquire_single_instance(app):
        print("Filect is already running — bringing the existing window to front.")
        sys.exit(0)
    
    # Set application icon (shows in taskbar and window title bar)
    # Try ICO first (for Windows), then PNG as fallback
    icon_path = source_root / 'resources' / 'iconnn.ico'
    if not icon_path.exists():
        icon_path = source_root / 'resources' / 'icon.png'
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    
    # Apply saved theme (dark/light)
    try:
        from app.ui.theme_manager import theme_manager
        theme_manager.apply_theme()
    except Exception as e:
        print(f"Failed to apply theme: {e}")
    
    # Check if Supabase is available
    if not SUPABASE_AVAILABLE:
        QMessageBox.warning(
            None,
            "Missing Dependency",
            "The 'supabase' package is required.\n\nPlease run: pip install supabase"
        )
        sys.exit(1)
    
    # Check for existing valid session BEFORE showing auth dialog
    has_valid_session = check_existing_session()
    
    if not has_valid_session:
        # Show auth dialog. Switch to Regular activation policy so the login
        # window has normal focus (releases when clicking other apps), then
        # restore the Accessory/agent policy once the dialog is done.
        auth_dialog = AuthDialog()
        app.set_auth_dialog(auth_dialog)
        app.set_normal_focus_mode(True)
        auth_result = auth_dialog.exec()
        app.set_normal_focus_mode(False)
        
        # If dialog was rejected (closed without auth), exit
        if auth_result == 0:  # QDialog.Rejected
            sub_check = supabase_auth.check_subscription()
            if not sub_check.get('has_subscription'):
                sys.exit(0)  # Exit cleanly if no subscription
    
    # Create and show main window
    window = MainWindow()
    app.set_main_window(window)  # register for filect:// deep link handling
    window.show()

    # Pull the app to the front now that the main window exists. After a Google
    # sign-in (active subscription) the browser had focus, so without this the
    # app stays buried behind the browser tab — _bring_to_front's policy flip
    # surfaces it reliably for an accessory (LSUIElement) app.
    app._bring_to_front()

    # Start event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
