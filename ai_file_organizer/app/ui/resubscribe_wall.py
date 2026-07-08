"""
Resubscribe wall — the paywall shown when the authoritative get_entitlement()
RPC reports that the signed-in user is no longer entitled.

This is purely a UX gate. The openai-proxy edge function already returns 403 for
any AI action by a lapsed user; without this screen those actions just fail with
a raw error and no explanation. The wall gives a clear message, a one-click route
back to the app's existing web checkout, and a Log out option. It polls
entitlement while shown, so a successful resubscribe (or any recovery) dismisses
it automatically without a restart.

No billing logic lives here. It only reads SupabaseAuth.get_entitlement() and
opens the existing checkout via SupabaseAuth.open_web_pricing().
"""

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton,
)

from app.core.supabase_client import supabase_auth

logger = logging.getLogger(__name__)


class ResubscribeWall(QDialog):
    """Blocking paywall for a user whose subscription has lapsed."""

    # exec() return codes. QDialog.Rejected (0, closing the window) == "quit".
    RESULT_RESUBSCRIBED = 10
    RESULT_LOGOUT = 11

    POLL_INTERVAL_MS = 5000
    POLL_LIMIT = 360  # ~30 min, mirrors the auth dialog's checkout polling

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filect")
        self.setObjectName("resubscribeWall")
        self.setModal(True)
        self.setFixedSize(480, 600)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)

        self._poll_count = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_entitlement)

        self._build_ui()

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        try:
            from app.ui.theme_manager import apply_titlebar_theme
            apply_titlebar_theme(self)
        except Exception:
            pass

    def closeEvent(self, event):
        self._poll_timer.stop()
        super().closeEvent(event)

    # -- theme -------------------------------------------------------------
    def _colors(self) -> dict:
        try:
            from app.ui.theme_manager import theme_manager
            return theme_manager.get_colors()
        except Exception:
            # Dark fallback matching the brand palette.
            return {
                'bg': '#0A0A12', 'card': '#16161F', 'border': '#1C1C28',
                'text': '#E8E8F0', 'text_secondary': '#B0B0C0',
                'danger_text': '#FF6B6B',
            }

    # -- UI ----------------------------------------------------------------
    def _build_ui(self):
        c = self._colors()
        self.setStyleSheet(f"""
            QDialog#resubscribeWall {{ background-color: {c['bg']}; }}
            QLabel {{ background: transparent; color: {c['text']}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 56, 48, 40)
        root.setSpacing(0)

        root.addStretch(1)

        # Lock badge
        badge = QLabel("🔒")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(84, 84)
        badge.setStyleSheet("""
            font-size: 38px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #7C4DFF, stop:1 #B39DFF);
            border-radius: 42px;
        """)
        badge_row = QHBoxLayout()
        badge_row.addStretch()
        badge_row.addWidget(badge)
        badge_row.addStretch()
        root.addLayout(badge_row)
        root.addSpacing(28)

        headline = QLabel("Your subscription has ended")
        headline.setAlignment(Qt.AlignCenter)
        headline.setWordWrap(True)
        headline.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {c['text']};")
        root.addWidget(headline)
        root.addSpacing(14)

        sub = QLabel(
            "Your Filect subscription is no longer active. "
            "Resubscribe to keep organizing and searching your files."
        )
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"font-size: 15px; color: {c['text_secondary']};")
        root.addWidget(sub)
        root.addSpacing(32)

        self.resubscribe_button = QPushButton("Resubscribe")
        self.resubscribe_button.setCursor(Qt.PointingHandCursor)
        self.resubscribe_button.setMinimumHeight(52)
        self.resubscribe_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7C4DFF, stop:1 #9575FF);
                color: white; border: none; border-radius: 12px;
                font-size: 16px; font-weight: 600;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #9575FF, stop:1 #B39DFF);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #6A3DE8, stop:1 #7C4DFF);
            }
        """)
        self.resubscribe_button.clicked.connect(self._resubscribe)
        root.addWidget(self.resubscribe_button)
        root.addSpacing(12)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(20)
        self.status_label.setStyleSheet(f"font-size: 13px; color: {c['text_secondary']};")
        root.addWidget(self.status_label)

        root.addStretch(2)

        self.logout_button = QPushButton("Log out")
        self.logout_button.setCursor(Qt.PointingHandCursor)
        self.logout_button.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none;
                color: {c['text_secondary']}; font-size: 14px; }}
            QPushButton:hover {{ color: {c['text']}; text-decoration: underline; }}
        """)
        self.logout_button.clicked.connect(self._logout)
        lo_row = QHBoxLayout()
        lo_row.addStretch()
        lo_row.addWidget(self.logout_button)
        lo_row.addStretch()
        root.addLayout(lo_row)

    # -- actions -----------------------------------------------------------
    def _resubscribe(self):
        """Open the existing web checkout and start polling for entitlement."""
        c = self._colors()
        self.status_label.setStyleSheet(f"font-size: 13px; color: {c['text_secondary']};")
        ok = supabase_auth.open_web_pricing()
        if ok:
            self.resubscribe_button.setText("Open checkout again")
            self.status_label.setText(
                "Finish checkout in your browser — this unlocks automatically "
                "once you're subscribed."
            )
            self._poll_count = 0
            self._poll_timer.start(self.POLL_INTERVAL_MS)
        else:
            self.status_label.setStyleSheet(f"font-size: 13px; color: {c['danger_text']};")
            self.status_label.setText("Couldn't open the checkout page. Please try again.")

    def _poll_entitlement(self):
        """While shown, re-check the source of truth so a resubscribe (or any
        recovery) dismisses the wall on its own."""
        self._poll_count += 1
        if self._poll_count > self.POLL_LIMIT:
            self._poll_timer.stop()
            self.status_label.setText("Still not active. Click Resubscribe when you're ready.")
            self.resubscribe_button.setText("Resubscribe")
            return

        try:
            ent = supabase_auth.get_entitlement()
        except Exception as e:
            logger.warning(f"Entitlement poll failed: {e}")
            return

        if ent.get('entitled'):
            self._poll_timer.stop()
            self.status_label.setText("You're all set! 🎉")
            QTimer.singleShot(600, lambda: self.done(self.RESULT_RESUBSCRIBED))

    def _logout(self):
        self._poll_timer.stop()
        self.done(self.RESULT_LOGOUT)

    # -- deep-link hooks ---------------------------------------------------
    # FilectApplication's filect:// handlers expect an AuthDialog. These
    # duck-typed methods let the wall stand in for it so a post-checkout deep
    # link (e.g. filect://open after paying) re-checks entitlement instead of
    # crashing on a missing attribute.
    def _check_subscription_silent(self):
        """A post-checkout deep link landed — re-check entitlement now."""
        self._poll_count = 0
        self._poll_entitlement()

    def _show_subscribe_page(self):
        """Deep-link asked to surface the paywall — we already are it."""
        self.raise_()
        self.activateWindow()
