"""Language card for the Voice page — a collapsible dropdown (auto-detect default, or
force a language).

Built as a click-to-expand header + an inline options grid, NOT a QComboBox/QMenu: in
this agent (LSUIElement) app any popup window (combo list, menu) opens on a non-active
Space and looks 'unclickable'. Inline buttons have no popup, so they always work.
settings.dictation_language: '' = auto-detect; else an ISO-639-1 code sent to Grok."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QPushButton, QButtonGroup, QWidget)

from app.core.settings import settings
from app.ui.theme_manager import get_theme_colors

ACCENT = "#7C4DFF"

# (label, code). '' = auto-detect (default). Grok-supported languages.
LANGUAGES = [
    ("Auto-detect", ""),
    ("English", "en"), ("Spanish", "es"), ("French", "fr"), ("German", "de"),
    ("Italian", "it"), ("Portuguese", "pt"), ("Dutch", "nl"), ("Greek", "el"),
    ("Russian", "ru"), ("Polish", "pl"), ("Turkish", "tr"), ("Arabic", "ar"),
    ("Hindi", "hi"), ("Chinese", "zh"), ("Japanese", "ja"), ("Korean", "ko"),
]
_COLS = 4


class VoiceLanguageCard(QFrame):
    """Settings-style card: a collapsible inline dropdown for the dictation language."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._c = get_theme_colors()
        c = self._c
        self._label_for = {code: label for label, code in LANGUAGES}

        self.setObjectName("settingsCard")
        self.setStyleSheet(f"""
            QFrame#settingsCard {{
                background-color: {c['surface']};
                border: 1px solid {c['border']};
                border-radius: 16px;
            }}
            QFrame#settingsCard > QLabel {{ border: none; background: transparent; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        from app.ui.icons import line_pixmap
        _trow = QHBoxLayout(); _trow.setSpacing(9)
        _ic = QLabel(); _ic.setPixmap(line_pixmap("globe", 16, ACCENT))
        _ic.setStyleSheet("background: transparent; border: none;")
        _trow.addWidget(_ic, 0, Qt.AlignVCenter)
        title = QLabel("Language")
        title.setStyleSheet(f"font-family: 'Sora', 'SF Pro Display', sans-serif; font-size: 15px; font-weight: 600; color: {ACCENT}; "
                            "background: transparent; border: none;")
        _trow.addWidget(title, 0, Qt.AlignVCenter)
        _trow.addStretch(1)
        layout.addLayout(_trow)

        hint = QLabel("Auto-detect works for most speech. Pick a specific language to force "
                      "transcription into only that one — helps for non-English or mixed speech.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {c['text_secondary']}; font-size: 13px; "
                           "background: transparent; border: none;")
        layout.addWidget(hint)

        # Click-to-expand header showing the current selection.
        self._header = QPushButton()
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setMinimumHeight(38)
        self._header.setStyleSheet(f"""
            QPushButton {{
                background-color: {c['input_bg']};
                border: 1px solid {c['border']};
                border-radius: 10px;
                color: {c['text']};
                text-align: left;
                padding: 0 14px;
                font-size: 13px;
            }}
            QPushButton:hover {{ border-color: {ACCENT}; }}
        """)
        self._header.clicked.connect(self._toggle)
        layout.addWidget(self._header)

        # Inline options grid, hidden until the header is clicked.
        self._options = QWidget()
        grid = QGridLayout(self._options)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 6, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        pill_style = f"""
            QPushButton {{
                background-color: {c['input_bg']};
                border: 1px solid {c['border']};
                border-radius: 14px;
                color: {c['text']};
                font-size: 12px;
                padding: 6px 10px;
            }}
            QPushButton:hover {{ border-color: {ACCENT}; }}
            QPushButton:checked {{ background-color: {ACCENT}; color: white; border-color: {ACCENT}; }}
        """
        current = getattr(settings, 'dictation_language', '') or ''
        for i, (label, code) in enumerate(LANGUAGES):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(32)
            btn.setStyleSheet(pill_style)
            btn.setChecked(code == current)
            btn.clicked.connect(lambda _checked, cd=code: self._select(cd))
            self._group.addButton(btn)
            grid.addWidget(btn, i // _COLS, i % _COLS)
        self._options.setVisible(False)
        layout.addWidget(self._options)

        self._expanded = False
        self._sync_header(current)

    def _toggle(self):
        self._expanded = not self._expanded
        self._options.setVisible(self._expanded)
        self._sync_header(getattr(settings, 'dictation_language', '') or '')

    def _sync_header(self, code: str):
        label = self._label_for.get(code, "Auto-detect")
        chevron = "▴" if self._expanded else "▾"
        self._header.setText(f"{label}        {chevron}")

    def _select(self, code: str):
        settings.dictation_language = code or ''
        settings._save_config()
        self._expanded = False
        self._options.setVisible(False)
        self._sync_header(code or '')
