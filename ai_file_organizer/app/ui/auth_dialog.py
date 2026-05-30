"""
Authentication dialog for login, signup, and subscription management.
Modern, clean design that adapts to dark/light theme.
"""

import logging
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QStackedWidget, QWidget, QMessageBox, QFrame,
    QGraphicsDropShadowEffect, QSpacerItem, QSizePolicy, QApplication
)
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont, QColor

from app.core.supabase_client import supabase_auth
from app.core.settings import settings

logger = logging.getLogger(__name__)


class EmailConfirmationDialog(QDialog):
    """
    Modern styled dialog for email confirmation notification.
    Shows after signup to tell user to check their email.
    Draggable and theme-aware.
    """
    
    def __init__(self, parent=None, email: str = ""):
        super().__init__(parent)
        self.email = email
        self._drag_pos = None
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(440)
        
        self._setup_ui()
    
    def mousePressEvent(self, event):
        """Enable dragging from anywhere on the dialog."""
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        """Handle dialog dragging."""
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """Stop dragging."""
        self._drag_pos = None
    
    def _create_step_row(self, number: str, text: str, c: dict) -> QWidget:
        """Create a step row with number badge and text."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 8, 0, 8)
        row_layout.setSpacing(14)
        
        # Number badge
        badge = QLabel(number)
        badge.setFixedSize(28, 28)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet("""
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #7C4DFF, stop:1 #9575FF);
            color: white;
            font-size: 13px;
            font-weight: 700;
            border-radius: 14px;
        """)
        row_layout.addWidget(badge)
        
        # Step text
        step_text = QLabel(text)
        step_text.setStyleSheet(f"""
            font-size: 14px;
            color: {c['text']};
            background: transparent;
            font-weight: 500;
        """)
        row_layout.addWidget(step_text, 1)
        
        return row

    def _setup_ui(self):
        from app.ui.theme_manager import get_theme_colors
        c = get_theme_colors()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Container with shadow
        container = QFrame()
        container.setObjectName("emailConfirmContainer")
        container.setStyleSheet(f"""
            QFrame#emailConfirmContainer {{
                background-color: {c['surface']};
                border: 1px solid {c['border']};
                border-radius: 20px;
            }}
        """)
        
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 80))
        container.setGraphicsEffect(shadow)
        
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(36, 40, 36, 36)
        container_layout.setSpacing(0)
        
        # Email icon with gradient background
        icon_container = QFrame()
        icon_container.setFixedSize(80, 80)
        icon_container.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #7C4DFF, stop:0.5 #9575FF, stop:1 #B39DFF);
                border-radius: 40px;
            }
        """)
        icon_layout = QVBoxLayout(icon_container)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        
        icon_label = QLabel("✉️")
        icon_label.setStyleSheet("font-size: 36px; background: transparent;")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_layout.addWidget(icon_label)
        
        # Center the icon
        icon_wrapper = QHBoxLayout()
        icon_wrapper.addStretch()
        icon_wrapper.addWidget(icon_container)
        icon_wrapper.addStretch()
        container_layout.addLayout(icon_wrapper)
        
        container_layout.addSpacing(24)
        
        # Title
        title = QLabel("Check Your Inbox!")
        title.setStyleSheet(f"""
            font-size: 24px;
            font-weight: 700;
            color: {c['text']};
            background: transparent;
        """)
        title.setAlignment(Qt.AlignCenter)
        container_layout.addWidget(title)
        
        container_layout.addSpacing(10)
        
        # Description
        desc1 = QLabel("We've sent a confirmation link to:")
        desc1.setStyleSheet(f"""
            font-size: 14px;
            color: {c['text_muted']};
            background: transparent;
        """)
        desc1.setAlignment(Qt.AlignCenter)
        container_layout.addWidget(desc1)
        
        container_layout.addSpacing(4)
        
        # Email label with purple highlight
        email_label = QLabel(self.email)
        email_label.setStyleSheet(f"""
            font-size: 15px;
            font-weight: 600;
            color: #7C4DFF;
            background: transparent;
        """)
        email_label.setAlignment(Qt.AlignCenter)
        email_label.setWordWrap(True)
        container_layout.addWidget(email_label)
        
        container_layout.addSpacing(24)
        
        # Steps card with clearer visual hierarchy
        steps_card = QFrame()
        steps_card.setStyleSheet(f"""
            QFrame {{
                background-color: {c['card']};
                border: 1px solid {c['border']};
                border-radius: 14px;
            }}
        """)
        steps_layout = QVBoxLayout(steps_card)
        steps_layout.setContentsMargins(20, 18, 20, 18)
        steps_layout.setSpacing(0)
        
        # Step 1
        step1_container = self._create_step_row("1", "Open your email inbox", c)
        steps_layout.addWidget(step1_container)
        
        # Divider
        divider1 = QFrame()
        divider1.setFixedHeight(1)
        divider1.setStyleSheet(f"background-color: {c['border']}; margin: 10px 0;")
        steps_layout.addWidget(divider1)
        
        # Step 2
        step2_container = self._create_step_row("2", "Click the verification link", c)
        steps_layout.addWidget(step2_container)
        
        # Divider
        divider2 = QFrame()
        divider2.setFixedHeight(1)
        divider2.setStyleSheet(f"background-color: {c['border']}; margin: 10px 0;")
        steps_layout.addWidget(divider2)
        
        # Step 3
        step3_container = self._create_step_row("3", "Come back and sign in", c)
        steps_layout.addWidget(step3_container)
        
        container_layout.addWidget(steps_card)
        
        container_layout.addSpacing(14)
        
        # Spam notice
        spam_notice = QLabel("💡 Don't see it? Check your spam folder")
        spam_notice.setStyleSheet(f"""
            font-size: 12px;
            color: {c['text_muted']};
            background: transparent;
        """)
        spam_notice.setAlignment(Qt.AlignCenter)
        container_layout.addWidget(spam_notice)
        
        container_layout.addSpacing(24)
        
        # OK button
        ok_btn = QPushButton("Got it!")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setMinimumHeight(48)
        ok_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7C4DFF, stop:1 #9575FF);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 15px;
                font-weight: 600;
                padding: 12px 24px;
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
        ok_btn.clicked.connect(self.accept)
        container_layout.addWidget(ok_btn)
        
        layout.addWidget(container)
    
    @staticmethod
    def show_confirmation(parent, email: str):
        """Show the email confirmation dialog."""
        dialog = EmailConfirmationDialog(parent, email)
        dialog.exec()


class ForgotPasswordDialog(QDialog):
    """
    Modern styled dialog for password reset.
    Allows user to enter email and receive reset link.
    """
    
    def __init__(self, parent=None, prefill_email: str = ""):
        super().__init__(parent)
        self.prefill_email = prefill_email
        self._sent = False
        self._drag_pos = None
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(420)
        
        self._setup_ui()
    
    def mousePressEvent(self, event):
        """Enable dragging."""
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        """Handle dragging."""
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """Stop dragging."""
        self._drag_pos = None
    
    def _setup_ui(self):
        from app.ui.theme_manager import get_theme_colors
        c = get_theme_colors()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Container
        self.container = QFrame()
        self.container.setObjectName("forgotPasswordContainer")
        self.container.setStyleSheet(f"""
            QFrame#forgotPasswordContainer {{
                background-color: {c['surface']};
                border: 1px solid {c['border']};
                border-radius: 20px;
            }}
        """)
        
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 80))
        self.container.setGraphicsEffect(shadow)
        
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(36, 40, 36, 36)
        self.container_layout.setSpacing(0)
        
        # Icon
        icon_container = QFrame()
        icon_container.setFixedSize(72, 72)
        icon_container.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #7C4DFF, stop:1 #B39DFF);
                border-radius: 36px;
            }
        """)
        icon_layout = QVBoxLayout(icon_container)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        
        icon_label = QLabel("🔑")
        icon_label.setStyleSheet("font-size: 32px; background: transparent;")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_layout.addWidget(icon_label)
        
        icon_wrapper = QHBoxLayout()
        icon_wrapper.addStretch()
        icon_wrapper.addWidget(icon_container)
        icon_wrapper.addStretch()
        self.container_layout.addLayout(icon_wrapper)
        
        self.container_layout.addSpacing(24)
        
        # Title
        self.title_label = QLabel("Reset Password")
        self.title_label.setStyleSheet(f"""
            font-size: 22px;
            font-weight: 700;
            color: {c['text']};
            background: transparent;
        """)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.container_layout.addWidget(self.title_label)
        
        self.container_layout.addSpacing(8)
        
        # Description
        self.desc_label = QLabel("Enter your email address and we'll send you a link to reset your password.")
        self.desc_label.setStyleSheet(f"""
            font-size: 14px;
            color: {c['text_muted']};
            background: transparent;
            line-height: 1.4;
        """)
        self.desc_label.setAlignment(Qt.AlignCenter)
        self.desc_label.setWordWrap(True)
        self.container_layout.addWidget(self.desc_label)
        
        self.container_layout.addSpacing(24)
        
        # Email input
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("Enter your email address")
        self.email_input.setText(self.prefill_email)
        self.email_input.setMinimumHeight(52)
        self.email_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {c['card']};
                border: 1px solid {c['border']};
                border-radius: 12px;
                padding: 12px 16px;
                font-size: 14px;
                color: {c['text']};
            }}
            QLineEdit:focus {{
                border: 2px solid #7C4DFF;
            }}
            QLineEdit::placeholder {{
                color: {c['text_muted']};
            }}
        """)
        self.container_layout.addWidget(self.email_input)
        
        self.container_layout.addSpacing(8)
        
        # Error/status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"""
            font-size: 13px;
            color: #FF6B6B;
            background: transparent;
        """)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(20)
        self.container_layout.addWidget(self.status_label)
        
        self.container_layout.addSpacing(16)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        # Cancel button
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setMinimumHeight(48)
        self.cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {c['text_muted']};
                border: 1px solid {c['border']};
                border-radius: 12px;
                font-size: 14px;
                font-weight: 500;
                padding: 12px 24px;
            }}
            QPushButton:hover {{
                background: {c['card']};
                color: {c['text']};
            }}
        """)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        # Send button
        self.send_btn = QPushButton("Send Reset Link")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setMinimumHeight(48)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7C4DFF, stop:1 #9575FF);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
                padding: 12px 24px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #9575FF, stop:1 #B39DFF);
            }
            QPushButton:disabled {
                background: #555;
                color: #888;
            }
        """)
        self.send_btn.clicked.connect(self._send_reset)
        btn_layout.addWidget(self.send_btn)
        
        self.container_layout.addLayout(btn_layout)
        
        layout.addWidget(self.container)
        
        # Connect enter key
        self.email_input.returnPressed.connect(self._send_reset)
    
    def _send_reset(self):
        """Send password reset email."""
        email = self.email_input.text().strip()
        
        if not email:
            self.status_label.setText("Please enter your email address")
            self.status_label.setStyleSheet("font-size: 13px; color: #FF6B6B; background: transparent;")
            return
        
        if '@' not in email:
            self.status_label.setText("Please enter a valid email address")
            self.status_label.setStyleSheet("font-size: 13px; color: #FF6B6B; background: transparent;")
            return
        
        self.send_btn.setEnabled(False)
        self.send_btn.setText("Sending...")
        self.status_label.setText("")
        
        # Send reset email via Supabase
        result = supabase_auth.reset_password(email)
        
        if result.get('success'):
            self._show_success(email)
        else:
            self.send_btn.setEnabled(True)
            self.send_btn.setText("Send Reset Link")
            self.status_label.setText(result.get('error', 'Failed to send reset email'))
            self.status_label.setStyleSheet("font-size: 13px; color: #FF6B6B; background: transparent;")
    
    def _show_success(self, email: str):
        """Transform dialog to show success state."""
        from app.ui.theme_manager import get_theme_colors
        c = get_theme_colors()
        
        self._sent = True
        
        # Update title and description
        self.title_label.setText("Check Your Email!")
        self.desc_label.setText(
            f"We've sent a password reset link to:\n\n"
            f"{email}\n\n"
            f"Click the link in the email to create a new password."
        )
        self.desc_label.setStyleSheet(f"""
            font-size: 14px;
            color: {c['text_muted']};
            background: transparent;
            line-height: 1.5;
        """)
        
        # Update status to success
        self.status_label.setText("💡 Don't see it? Check your spam folder")
        self.status_label.setStyleSheet(f"font-size: 12px; color: {c['text_muted']}; background: transparent;")
        
        # Hide email input
        self.email_input.hide()
        
        # Update buttons
        self.cancel_btn.hide()
        self.send_btn.setText("Done")
        self.send_btn.setEnabled(True)
        self.send_btn.clicked.disconnect()
        self.send_btn.clicked.connect(self.accept)
    
    @staticmethod
    def show_dialog(parent, prefill_email: str = ""):
        """Show the forgot password dialog."""
        dialog = ForgotPasswordDialog(parent, prefill_email)
        dialog.exec()


class AuthDialog(QDialog):
    """Authentication dialog for user login/signup and subscription."""
    
    # Signals
    auth_successful = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filect")
        # Taller now to fit the "or — Continue with Google" block in the login
        # page without clipping (was 680, fields were getting cut off).
        self.setFixedSize(460, 780)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowMinimizeButtonHint)
        self.setObjectName("authDialog")
        self._pending_email = ""

        self._setup_ui()
        self._setup_connections()
        
        # Subscription polling timer
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_subscription)
        self._poll_count = 0
        
        # Try to restore session on init
        QTimer.singleShot(100, self._try_restore_session)
    
    def showEvent(self, event):
        """Apply dark/light title bar when dialog is shown. Also reset the
        Google button so it never gets stuck in 'Waiting…' across show cycles
        (e.g., after sign-out from the in-app account page)."""
        super().showEvent(event)
        from app.ui.theme_manager import apply_titlebar_theme
        apply_titlebar_theme(self)
        if hasattr(self, 'google_login_button'):
            self._reset_google_button()
    
    def _setup_ui(self):
        """Set up the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Main container with card styling
        self.container = QFrame()
        self.container.setObjectName("authContainer")
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(48, 40, 48, 40)
        container_layout.setSpacing(0)
        
        # Header with logo
        header_layout = QVBoxLayout()
        header_layout.setSpacing(12)
        
        # Logo icon
        logo_label = QLabel("🔍")
        logo_label.setObjectName("authLogo")
        logo_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(logo_label)
        
        # Title
        self.title_label = QLabel("File Search Assistant")
        self.title_label.setObjectName("authTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.title_label)
        
        # Subtitle
        self.subtitle_label = QLabel("Sign in to your account")
        self.subtitle_label.setObjectName("authSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.subtitle_label)
        
        container_layout.addLayout(header_layout)
        container_layout.addSpacing(32)
        
        # Stacked widget for different views
        self.stack = QStackedWidget()
        self.stack.setObjectName("authStack")
        container_layout.addWidget(self.stack)
        
        # Create pages
        self._create_login_page()
        self._create_signup_page()
        self._create_subscribe_page()
        self._create_verify_page()
        
        layout.addWidget(self.container)
        
        # Start with login page
        self.stack.setCurrentIndex(0)
    
    def _create_input_field(self, label_text, placeholder, is_password=False):
        """Create a styled input field with label."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        label = QLabel(label_text)
        label.setObjectName("authLabel")
        layout.addWidget(label)
        
        input_field = QLineEdit()
        input_field.setPlaceholderText(placeholder)
        input_field.setObjectName("authInput")
        input_field.setMinimumHeight(52)
        if is_password:
            input_field.setEchoMode(QLineEdit.Password)
        
        layout.addWidget(input_field)
        
        return container, input_field
    
    def _create_login_page(self):
        """Create the login page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        
        # Email field
        email_container, self.login_email = self._create_input_field(
            "Email", "Enter your email address"
        )
        layout.addWidget(email_container)
        
        # Password field
        password_container, self.login_password = self._create_input_field(
            "Password", "Enter your password", is_password=True
        )
        layout.addWidget(password_container)

        layout.addSpacing(8)

        # Login button
        self.login_button = QPushButton("Sign In")
        self.login_button.setObjectName("primaryButton")
        self.login_button.setMinimumHeight(52)
        self.login_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.login_button)

        # ── Continue with Google ────────────────────────────────────────────
        # Five users so far signed up via Google on the web (filect.io/signup).
        # Without this they can't log into the desktop app at all (they have
        # no password). Opens system browser → Supabase OAuth → returns via
        # filect://auth-callback deep link handled in main.py.
        layout.addSpacing(8)
        or_layout = QHBoxLayout()
        or_left = QFrame(); or_left.setFrameShape(QFrame.HLine); or_left.setObjectName("authDivider"); or_left.setFixedHeight(1)
        or_right = QFrame(); or_right.setFrameShape(QFrame.HLine); or_right.setObjectName("authDivider"); or_right.setFixedHeight(1)
        or_label = QLabel("or")
        or_label.setStyleSheet("color: palette(mid); font-size: 12px; padding: 0 10px;")
        or_layout.addWidget(or_left, 1); or_layout.addWidget(or_label); or_layout.addWidget(or_right, 1)
        layout.addLayout(or_layout)
        layout.addSpacing(8)

        self.google_login_button = QPushButton("  Continue with Google")
        self.google_login_button.setMinimumHeight(48)
        self.google_login_button.setCursor(Qt.PointingHandCursor)
        # Google's brand guidance: white button, dark text, subtle border.
        self.google_login_button.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #1F1F1F;
                border: 1px solid #DADCE0;
                border-radius: 10px;
                font-size: 14px;
                font-weight: 600;
                text-align: center;
            }
            QPushButton:hover  { background-color: #F8F9FA; }
            QPushButton:disabled { color: #888; background-color: #f0f0f0; }
        """)
        # Inline Google "G" SVG so we don't depend on an external icon asset.
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtCore import QByteArray, QSize
        google_g_svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
          <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8a12 12 0 1 1 0-24c3 0 5.8 1.1 7.9 3l5.7-5.7C33.6 6.1 29.1 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/>
          <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 16 18.9 13 24 13c3 0 5.8 1.1 7.9 3l5.7-5.7C33.6 6.1 29.1 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>
          <path fill="#4CAF50" d="M24 44c5 0 9.6-1.9 13-5.1l-6-5.1c-2 1.5-4.5 2.4-7 2.4-5.3 0-9.7-3.4-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/>
          <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4 5.5l6 5.1c-.4.4 6.7-4.9 6.7-14.6 0-1.3-.1-2.4-.4-3.5z"/>
        </svg>'''
        pix = QPixmap(); pix.loadFromData(QByteArray(google_g_svg))
        self.google_login_button.setIcon(QIcon(pix))
        self.google_login_button.setIconSize(QSize(18, 18))
        self.google_login_button.clicked.connect(self._sign_in_with_google)
        layout.addWidget(self.google_login_button)
        
        # Error label
        self.login_error = QLabel("")
        self.login_error.setObjectName("errorLabel")
        self.login_error.setAlignment(Qt.AlignCenter)
        self.login_error.setWordWrap(True)
        self.login_error.setMinimumHeight(24)
        layout.addWidget(self.login_error)
        
        # Forgot password link
        forgot_layout = QHBoxLayout()
        forgot_layout.addStretch()
        self.forgot_password_btn = QPushButton("Forgot password?")
        self.forgot_password_btn.setObjectName("linkButton")
        self.forgot_password_btn.setCursor(Qt.PointingHandCursor)
        forgot_layout.addWidget(self.forgot_password_btn)
        forgot_layout.addStretch()
        layout.addLayout(forgot_layout)
        
        layout.addStretch()
        
        # Divider
        divider = QFrame()
        divider.setObjectName("authDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        
        layout.addSpacing(16)
        
        # Switch to signup
        switch_layout = QHBoxLayout()
        switch_layout.setSpacing(4)
        switch_label = QLabel("Don't have an account?")
        switch_label.setObjectName("authSwitchLabel")
        self.to_signup_button = QPushButton("Create account")
        self.to_signup_button.setObjectName("linkButton")
        self.to_signup_button.setCursor(Qt.PointingHandCursor)
        switch_layout.addStretch()
        switch_layout.addWidget(switch_label)
        switch_layout.addWidget(self.to_signup_button)
        switch_layout.addStretch()
        layout.addLayout(switch_layout)
        
        self.stack.addWidget(page)
    
    def _create_signup_page(self):
        """Create the signup page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Email field
        email_container, self.signup_email = self._create_input_field(
            "Email", "Enter your email address"
        )
        layout.addWidget(email_container)
        
        # Password field
        password_container, self.signup_password = self._create_input_field(
            "Password", "Create a password (min 6 chars)", is_password=True
        )
        layout.addWidget(password_container)
        
        # Confirm Password field
        confirm_container, self.signup_confirm = self._create_input_field(
            "Confirm Password", "Confirm your password", is_password=True
        )
        layout.addWidget(confirm_container)
        
        layout.addSpacing(4)
        
        # Signup button
        self.signup_button = QPushButton("Create Account")
        self.signup_button.setObjectName("primaryButton")
        self.signup_button.setMinimumHeight(52)
        self.signup_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.signup_button)
        
        # Error label
        self.signup_error = QLabel("")
        self.signup_error.setObjectName("errorLabel")
        self.signup_error.setAlignment(Qt.AlignCenter)
        self.signup_error.setWordWrap(True)
        self.signup_error.setMinimumHeight(24)
        layout.addWidget(self.signup_error)
        
        layout.addStretch()
        
        # Divider
        divider = QFrame()
        divider.setObjectName("authDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        
        layout.addSpacing(16)
        
        # Switch to login
        switch_layout = QHBoxLayout()
        switch_layout.setSpacing(4)
        switch_label = QLabel("Already have an account?")
        switch_label.setObjectName("authSwitchLabel")
        self.to_login_button = QPushButton("Sign in")
        self.to_login_button.setObjectName("linkButton")
        self.to_login_button.setCursor(Qt.PointingHandCursor)
        switch_layout.addStretch()
        switch_layout.addWidget(switch_label)
        switch_layout.addWidget(self.to_login_button)
        switch_layout.addStretch()
        layout.addLayout(switch_layout)
        
        self.stack.addWidget(page)
    
    def _create_subscribe_page(self):
        """Create the subscription page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Welcome message
        self.welcome_label = QLabel("Welcome!")
        self.welcome_label.setObjectName("authWelcome")
        self.welcome_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.welcome_label)
        
        layout.addSpacing(8)
        
        # Subscription card
        sub_card = QFrame()
        sub_card.setObjectName("subscriptionCard")
        card_layout = QVBoxLayout(sub_card)
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(16)
        
        # Plan badge
        plan_badge = QLabel("CHOOSE YOUR PLAN")
        plan_badge.setObjectName("planBadge")
        plan_badge.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(plan_badge)

        # Price
        price_layout = QHBoxLayout()
        price_layout.setAlignment(Qt.AlignCenter)
        price_layout.setSpacing(4)

        price_amount = QLabel("from $15")
        price_amount.setObjectName("priceAmount")
        price_period = QLabel("/ mo")
        price_period.setObjectName("pricePeriod")
        
        price_layout.addWidget(price_amount)
        price_layout.addWidget(price_period)
        card_layout.addLayout(price_layout)
        
        # Features - just 2 to fit the space
        features = [
            "✓  AI-powered search & organization",
            "✓  Auto-indexing, OCR & Vision AI",
        ]
        
        features_container = QWidget()
        features_container.setObjectName("featuresContainer")
        features_layout = QVBoxLayout(features_container)
        features_layout.setContentsMargins(8, 12, 8, 8)
        features_layout.setSpacing(8)
        
        for feature in features:
            feat_label = QLabel(feature)
            feat_label.setObjectName("featureLabel")
            feat_label.setMinimumHeight(24)
            features_layout.addWidget(feat_label)
        
        card_layout.addWidget(features_container)
        layout.addWidget(sub_card)
        
        layout.addSpacing(16)
        
        # Subscribe button
        self.subscribe_button = QPushButton("View plans & subscribe")
        self.subscribe_button.setObjectName("primaryButton")
        self.subscribe_button.setMinimumHeight(52)
        self.subscribe_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.subscribe_button)
        
        # Status label
        self.sub_status = QLabel("")
        self.sub_status.setObjectName("statusLabel")
        self.sub_status.setAlignment(Qt.AlignCenter)
        self.sub_status.setWordWrap(True)
        self.sub_status.setMinimumHeight(24)
        layout.addWidget(self.sub_status)
        
        layout.addStretch()
        
        # Logout
        self.logout_button = QPushButton("Sign Out")
        self.logout_button.setObjectName("linkButton")
        self.logout_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.logout_button, alignment=Qt.AlignCenter)
        
        self.stack.addWidget(page)

    def _create_verify_page(self):
        """Email code-verification page, shown after signup (code-only email)."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setSpacing(16)

        header = QLabel("Check your email")
        header.setObjectName("authWelcome")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)

        self.verify_message = QLabel(
            "We sent a 6-digit code to your email. Enter it below to verify your account."
        )
        self.verify_message.setObjectName("authSwitchLabel")
        self.verify_message.setAlignment(Qt.AlignCenter)
        self.verify_message.setWordWrap(True)
        layout.addWidget(self.verify_message)

        layout.addSpacing(8)

        code_container, self.verify_code = self._create_input_field(
            "Verification code", "Enter 6-digit code"
        )
        layout.addWidget(code_container)

        self.verify_button = QPushButton("Verify & Continue")
        self.verify_button.setObjectName("primaryButton")
        self.verify_button.setMinimumHeight(52)
        self.verify_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.verify_button)

        self.verify_error = QLabel("")
        self.verify_error.setObjectName("errorLabel")
        self.verify_error.setAlignment(Qt.AlignCenter)
        self.verify_error.setWordWrap(True)
        self.verify_error.setMinimumHeight(24)
        layout.addWidget(self.verify_error)

        layout.addStretch()

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self.resend_code_button = QPushButton("Resend code")
        self.resend_code_button.setObjectName("linkButton")
        self.resend_code_button.setCursor(Qt.PointingHandCursor)
        self.verify_back_button = QPushButton("Back to sign in")
        self.verify_back_button.setObjectName("linkButton")
        self.verify_back_button.setCursor(Qt.PointingHandCursor)
        bottom.addStretch()
        bottom.addWidget(self.resend_code_button)
        bottom.addWidget(self.verify_back_button)
        bottom.addStretch()
        layout.addLayout(bottom)

        self.stack.addWidget(page)

    def _setup_connections(self):
        """Set up signal connections."""
        # Login page
        self.login_button.clicked.connect(self._do_login)
        self.login_password.returnPressed.connect(self._do_login)
        self.to_signup_button.clicked.connect(self._go_to_signup)
        self.forgot_password_btn.clicked.connect(self._show_forgot_password)
        
        # Signup page
        self.signup_button.clicked.connect(self._do_signup)
        self.signup_confirm.returnPressed.connect(self._do_signup)
        self.to_login_button.clicked.connect(self._go_to_login)
        
        # Subscribe page
        self.subscribe_button.clicked.connect(self._open_checkout)
        self.logout_button.clicked.connect(self._do_logout)

        # Verify page
        self.verify_button.clicked.connect(self._do_verify_code)
        self.verify_code.returnPressed.connect(self._do_verify_code)
        self.resend_code_button.clicked.connect(self._resend_code)
        self.verify_back_button.clicked.connect(self._go_to_login)
    
    def _show_forgot_password(self):
        """Show the forgot password dialog."""
        # Pre-fill with email from login field if available
        email = self.login_email.text().strip()
        ForgotPasswordDialog.show_dialog(self, email)
    
    def _go_to_signup(self):
        """Navigate to signup page."""
        self.title_label.setText("Create Account")
        self.subtitle_label.setText("Sign up to get started")
        self.stack.setCurrentIndex(1)
    
    def _go_to_login(self):
        """Navigate to login page."""
        self.title_label.setText("File Search Assistant")
        self.subtitle_label.setText("Sign in to your account")
        self.stack.setCurrentIndex(0)

    def _go_to_verify(self, email):
        """Show the code-verification page after signup."""
        self._pending_email = email
        self.title_label.setText("Verify your email")
        self.subtitle_label.setText("One quick step to finish")
        self.verify_message.setText(
            f"We sent a 6-digit code to {email}. Enter it below to verify your account."
        )
        self.verify_code.clear()
        self.verify_error.setText("")
        self.stack.setCurrentIndex(3)
        self.verify_code.setFocus()

    def _do_verify_code(self):
        """Verify the 6-digit signup code, then continue."""
        code = self.verify_code.text().strip()
        if not code:
            self.verify_error.setText("Enter the code from your email")
            return

        self.verify_button.setEnabled(False)
        self.verify_button.setText("Verifying...")

        result = supabase_auth.verify_signup_code(self._pending_email, code)

        self.verify_button.setEnabled(True)
        self.verify_button.setText("Verify & Continue")

        if result.get('success'):
            self.verify_error.setText("")
            tokens = supabase_auth.get_session_tokens()
            if tokens:
                settings.set_auth_tokens(
                    tokens['access_token'],
                    tokens['refresh_token'],
                    self._pending_email
                )
            self._check_subscription_silent()
        else:
            self.verify_error.setText("That code is incorrect or expired. Check your email and try again.")

    def _resend_code(self):
        """Resend the signup confirmation code."""
        result = supabase_auth.resend_signup_code(self._pending_email)
        if result.get('success'):
            self.verify_error.setText("")
            self.verify_message.setText(f"New code sent to {self._pending_email}. Enter it below.")
        else:
            self.verify_error.setText("Could not resend the code. Please try again.")
    
    def _try_restore_session(self):
        """Try to restore a previous session."""
        if settings.has_stored_session():
            result = supabase_auth.restore_session(
                settings.auth_access_token,
                settings.auth_refresh_token
            )

            if result.get('success'):
                logger.info("Session restored successfully")
                # Save the new tokens Supabase issued so next startup uses fresh ones
                tokens = supabase_auth.get_session_tokens()
                if tokens:
                    settings.set_auth_tokens(
                        tokens['access_token'],
                        tokens['refresh_token'],
                        settings.auth_user_email
                    )
                self._check_subscription_silent()
            else:
                settings.clear_auth_tokens()
    
    def _do_login(self):
        """Handle login button click."""
        email = self.login_email.text().strip()
        password = self.login_password.text()
        
        if not email or not password:
            self.login_error.setText("Please enter email and password")
            return
        
        self.login_button.setEnabled(False)
        self.login_button.setText("Signing in...")
        
        result = supabase_auth.sign_in(email, password)
        
        self.login_button.setEnabled(True)
        self.login_button.setText("Sign In")
        
        if result.get('success'):
            self.login_error.setText("")
            tokens = supabase_auth.get_session_tokens()
            if tokens:
                settings.set_auth_tokens(
                    tokens['access_token'],
                    tokens['refresh_token'],
                    email
                )
            self._check_subscription_silent()
        else:
            error = result.get('error', 'Login failed')
            self.login_error.setText(error)
    
    def _do_signup(self):
        """Handle signup button click."""
        email = self.signup_email.text().strip()
        password = self.signup_password.text()
        confirm = self.signup_confirm.text()
        
        if not email or not password:
            self.signup_error.setText("Please fill all fields")
            return
        
        if password != confirm:
            self.signup_error.setText("Passwords don't match")
            return
        
        if len(password) < 6:
            self.signup_error.setText("Password must be at least 6 characters")
            return
        
        self.signup_button.setEnabled(False)
        self.signup_button.setText("Creating account...")
        
        result = supabase_auth.sign_up(email, password)
        
        self.signup_button.setEnabled(True)
        self.signup_button.setText("Create Account")
        
        if result.get('success'):
            self.signup_error.setText("")
            
            if result.get('needs_confirmation'):
                self._go_to_verify(email)
            else:
                # Account created and auto-logged in
                tokens = supabase_auth.get_session_tokens()
                if tokens:
                    settings.set_auth_tokens(
                        tokens['access_token'],
                        tokens['refresh_token'],
                        email
                    )
                # Check if they already have a subscription (e.g., from a previous signup)
                self._check_subscription_silent()
        else:
            error = result.get('error', 'Signup failed')
            self.signup_error.setText(error)
    
    def _show_subscribe_page(self):
        """Show the subscription page."""
        email = supabase_auth.user_email or settings.auth_user_email
        short_email = email.split('@')[0] if email else "there"
        self.welcome_label.setText(f"Hey {short_email}! 👋")
        self.title_label.setText("Unlock Filect")
        self.subtitle_label.setText("Choose a plan to access all features")
        self.stack.setCurrentIndex(2)
    
    def _open_checkout(self):
        """Open the web pricing page (plan selection + payment) and start polling."""
        self.sub_status.setObjectName("statusLabel")
        self.sub_status.setStyleSheet("")

        success = supabase_auth.open_web_pricing()

        if success:
            self.subscribe_button.setText("View plans again")
            self.sub_status.setText("Choose a plan in your browser — the app unlocks once you subscribe.")
            # Restart polling from zero each time the pricing page is opened
            self._poll_count = 0
            self._poll_timer.start(3000)
        else:
            self.subscribe_button.setText("View plans & subscribe")
            self.sub_status.setText("Couldn't open the plans page. Try again.")
            self.sub_status.setObjectName("errorLabel")
    
    def _poll_subscription(self):
        """Poll for subscription status after checkout."""
        self._poll_count += 1

        # Silent timeout after 30 minutes — reset button text, stop polling
        if self._poll_count > 600:
            self._poll_timer.stop()
            self.subscribe_button.setText("Subscribe Now")
            self.sub_status.setText("")
            return

        result = supabase_auth.check_subscription()

        if result.get('has_subscription'):
            self._poll_timer.stop()
            self.sub_status.setText("Payment confirmed! 🎉")
            logger.info("Subscription verified!")
            QTimer.singleShot(500, lambda: (self.auth_successful.emit(), self.accept()))
    
    def _sign_in_with_google(self):
        """Launch Google OAuth via the loopback-server pattern (RFC 8252).

        Filect:// deep links don't work reliably from PyInstaller .app bundles
        — Apple Events get dropped before our handler fires. So we use the
        standard desktop-OAuth approach instead: spin up a tiny local HTTP
        server, point Supabase's redirect_to at it, capture the tokens from
        the URL fragment via a JS shim, install the session, shut down.
        Same pattern Slack/GitHub Desktop/AWS CLI use.
        """
        import webbrowser, threading, socket
        from urllib.parse import quote
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from app.core.supabase_client import SUPABASE_URL

        PORT = 53682  # Registered with Supabase's URI_ALLOW_LIST.

        # Pre-flight bind to surface "port truly in use" vs "we just used it 10s
        # ago and the kernel hasn't released it" (TCP TIME_WAIT, ~60s). SO_REUSEADDR
        # lets us rebind through the TIME_WAIT — without it, a second sign-in
        # attempt minutes after the first one wrongly errors.
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.bind(('127.0.0.1', PORT))
            test_sock.close()
        except OSError:
            self.login_error.setText(f"Port {PORT} is in use. Quit other Filect instances and try again.")
            return

        self.google_login_button.setText("  Waiting for Google sign-in…")
        self.google_login_button.setEnabled(False)
        self.login_error.setText("")

        # Captured here, handed back to the Qt thread via QTimer.singleShot.
        token_box = {"access": None, "refresh": None, "error": None}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass  # silence per-request console spam

            def do_GET(self):
                # Step 1: browser lands on /callback. Tokens are in the URL
                # FRAGMENT (after #) which the server NEVER receives — only the
                # browser sees it. So we serve a tiny HTML page whose JS reads
                # the fragment and POSTs it back to /tokens on the same server.
                if self.path.startswith("/callback"):
                    html = b"""<!doctype html><html><head><meta charset="utf-8"><title>Signing you in...</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0f0f1a;color:#fff}.card{background:#1a1a2e;border:1px solid #2a2a3f;border-radius:12px;padding:32px 40px;text-align:center;max-width:380px}h1{font-size:20px;margin:0 0 10px;color:#fff}p{color:#a1a1b5;font-size:14px;margin:0;line-height:1.5}.ok{color:#4ADE80;font-size:32px;margin-bottom:8px}</style></head>
<body><div class="card" id="card"><h1>Signing you in...</h1><p>One moment.</p></div>
<script>
(async function(){
  const h = new URLSearchParams(window.location.hash.slice(1));
  const at = h.get('access_token'); const rt = h.get('refresh_token'); const err = h.get('error_description');
  try {
    await fetch('/tokens', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({access_token: at, refresh_token: rt, error: err})});
  } catch(e) {}
  const card = document.getElementById('card');
  if (at && rt) {
    card.innerHTML = '<div class="ok">checkmark</div><h1>You\\'re signed in.</h1><p>You can close this tab and return to Filect.</p>';
    card.querySelector('.ok').textContent = '\\u2713';
  } else {
    card.innerHTML = '<h1>Sign-in failed</h1><p>' + (err || 'Please try again from the app.') + '</p>';
  }
})();
</script></body></html>"""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                else:
                    self.send_response(404); self.end_headers()

            def do_POST(self):
                if self.path == "/tokens":
                    import json as _json
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    body = self.rfile.read(n).decode("utf-8", errors="ignore") if n else "{}"
                    try:
                        data = _json.loads(body)
                    except Exception:
                        data = {}
                    token_box["access"] = data.get("access_token")
                    token_box["refresh"] = data.get("refresh_token")
                    token_box["error"] = data.get("error")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(404); self.end_headers()

        # Subclass that sets SO_REUSEADDR before bind — same reason as the
        # pre-flight check above. Without this, the HTTPServer itself would
        # fail to bind when re-used quickly after sign-out → sign-in.
        class ReuseHTTPServer(HTTPServer):
            allow_reuse_address = True

        server = ReuseHTTPServer(("127.0.0.1", PORT), Handler)
        # Stash so we can shut down deterministically from the polling timer.
        self._google_server = server

        def _serve():
            try:
                server.serve_forever()
            except Exception:
                pass

        threading.Thread(target=_serve, daemon=True, name="google-oauth-loopback").start()

        # Open browser to Supabase OAuth, telling it to redirect to our loopback.
        redirect = quote(f"http://127.0.0.1:{PORT}/callback", safe='')
        url = f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={redirect}"
        try:
            # webbrowser.open() on macOS doesn't reliably activate the browser
            # when our app currently has focus — the tab opens but stays behind
            # the Filect window. `open <url>` (the macOS shell command) does
            # activate the browser. Fall back to webbrowser.open on other OSes.
            import subprocess, sys as _sys
            if _sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                webbrowser.open(url)
        except Exception as e:
            logger.error(f"Could not open browser for Google sign-in: {e}")
            self._stop_google_server()
            self.login_error.setText("Could not open browser. Please try again.")
            self.google_login_button.setText("  Continue with Google")
            self.google_login_button.setEnabled(True)
            return

        # Poll for tokens on the Qt thread so we can touch UI safely. Times out
        # after 5 minutes — enough for any reasonable Google sign-in flow.
        self._google_token_box = token_box
        self._google_deadline = QTimer(self)
        self._google_deadline.setSingleShot(True)
        self._google_deadline.timeout.connect(lambda: self._google_timeout())
        self._google_deadline.start(5 * 60 * 1000)

        self._google_poll = QTimer(self)
        self._google_poll.timeout.connect(self._google_check_tokens)
        self._google_poll.start(200)

    def _google_check_tokens(self):
        box = getattr(self, "_google_token_box", None)
        if not box:
            return
        if box.get("error"):
            self._stop_google_server()
            self.login_error.setText(f"Google sign-in failed: {box['error']}")
            self._reset_google_button()
            return
        if box.get("access") and box.get("refresh"):
            self._stop_google_server()
            logger.info("Google OAuth: installing session from loopback callback")
            result = supabase_auth.restore_session(box["access"], box["refresh"])
            if not result.get("success"):
                self.login_error.setText(f"Could not install session: {result.get('error')}")
                self._reset_google_button()
                return
            tokens = supabase_auth.get_session_tokens()
            if tokens:
                settings.set_auth_tokens(
                    tokens['access_token'], tokens['refresh_token'],
                    supabase_auth.user_email or ''
                )
            # Bring the app to the foreground so the user doesn't have to manually
            # switch back from the browser tab. As an LSUIElement (accessory) app
            # macOS throttles activateIgnoringOtherApps_ — sometimes it works,
            # sometimes it dampens. The fix: temporarily flip to Regular activation
            # policy, activate, then flip back to Accessory once the window is up.
            try:
                app = QApplication.instance()
                if app is not None and hasattr(app, 'set_normal_focus_mode'):
                    app.set_normal_focus_mode(True)
                if app is not None and hasattr(app, '_bring_to_front'):
                    app._bring_to_front()
                self.raise_()
                self.activateWindow()
                # Flip back to Accessory after the activation has settled. 600ms
                # is enough for Cocoa to register the activation; flipping too
                # fast can race and cancel it.
                if app is not None and hasattr(app, 'set_normal_focus_mode'):
                    QTimer.singleShot(600, lambda: app.set_normal_focus_mode(False))
            except Exception:
                pass
            # Reset the button state so if the user ends up back at the login
            # page (e.g., they had no subscription → signed out from sub page),
            # the Google button isn't stuck on "Waiting…".
            self._reset_google_button()
            self._check_subscription_silent()

    def _google_timeout(self):
        self._stop_google_server()
        if not supabase_auth.is_authenticated:
            self.login_error.setText("Google sign-in timed out. Please try again.")
            self._reset_google_button()

    def _stop_google_server(self):
        for attr in ("_google_poll", "_google_deadline"):
            t = getattr(self, attr, None)
            if t is not None:
                try: t.stop()
                except Exception: pass
        srv = getattr(self, "_google_server", None)
        if srv is not None:
            try: srv.shutdown(); srv.server_close()
            except Exception: pass
        self._google_server = None
        self._google_token_box = None

    def _reset_google_button(self):
        # Unconditional reset — used in three cases:
        #   1) after successful OAuth (button is no longer "waiting", we're done)
        #   2) on timeout or error
        #   3) every time the auth dialog is shown (in case it was left in
        #      "Waiting…" state by a previous flow + sign-out)
        if hasattr(self, 'google_login_button'):
            self.google_login_button.setText("  Continue with Google")
            self.google_login_button.setEnabled(True)

    def _check_subscription_silent(self):
        """Check subscription without UI updates."""
        result = supabase_auth.check_subscription()

        if result.get('has_subscription'):
            logger.info("Active subscription found")
            self.auth_successful.emit()
            self.accept()
        elif result.get('trial_blocked_reason') == 'duplicate_trial_card':
            # User tried to start a trial with a card that was already used.
            # Route to /trial-blocked (clear message + "$X immediately" CTA)
            # instead of the generic pricing page so they know what happened.
            logger.info("Trial blocked (duplicate card) — routing to /trial-blocked")
            self._show_subscribe_page()
            QTimer.singleShot(150, self._open_trial_blocked)
        else:
            # No subscription → switch to the subscribe page in the dialog AND
            # auto-open filect.io/pricing in the browser so the user lands on
            # the full pricing layout without an extra click. The in-app page
            # stays as a fallback canvas with a "view plans again" button +
            # subscription polling — same UX as before, just front-loaded.
            self._show_subscribe_page()
            QTimer.singleShot(150, self._open_checkout)

    def _open_trial_blocked(self):
        """Open the /trial-blocked page in browser + update the in-app
        subscribe page text so it matches what just happened."""
        supabase_auth.open_trial_blocked_page()
        # Make the in-app dialog text honest about why we routed here.
        try:
            self.subscribe_button.setText("View options")
            self.sub_status.setText(
                "Your free trial isn't available — this card was already used for a prior trial. "
                "We've opened the next steps in your browser."
            )
            # Still poll subscription so when they pay, the app auto-unlocks.
            self._poll_count = 0
            self._poll_timer.start(3000)
        except Exception:
            pass
    
    def _do_logout(self):
        """Handle logout."""
        supabase_auth.sign_out()
        settings.clear_auth_tokens()
        
        self.login_email.clear()
        self.login_password.clear()
        self.signup_email.clear()
        self.signup_password.clear()
        self.signup_confirm.clear()
        
        self._go_to_login()
    
    def closeEvent(self, event):
        """Handle dialog close."""
        if self._poll_timer.isActive():
            self._poll_timer.stop()
        event.accept()
