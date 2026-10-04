"""Headless proof that every Voice-page card re-themes on a runtime toggle.

Each voice card captured its colours once in __init__, so a dark/light toggle
left some cards stranded in the old theme. Each card now exposes
``apply_theme(theme)`` that re-reads get_theme_colors() and re-applies its inline
stylesheets. This test instantiates each card and asserts its frame background
flips to the expected surface/card hex for 'light' and 'dark'.

Run:
  cd <repo> && QT_QPA_PLATFORM=offscreen FILECT_DEV=1 \
    PYTHONPATH="$PWD/ai_file_organizer" ./venv311/bin/python test_theme_toggle.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FILECT_DEV", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ai_file_organizer"))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv)

from app.ui.voice_cards.custom_words_card import VoiceCustomWordsCard
from app.ui.voice_cards.language_card import VoiceLanguageCard
from app.ui.voice_cards.cleanup_card import VoiceCleanupCard
from app.ui.voice_cards.mute_card import VoiceMuteCard
from app.ui.voice_cards.history_card import VoiceHistoryCard

# Dark surface=#111119, card=#16161F; light surface=card=#FFFFFF.
DARK_HEXES = ("#16161F", "#111119")
LIGHT_HEX = "#FFFFFF"

CARDS = [
    ("VoiceCustomWordsCard", VoiceCustomWordsCard),
    ("VoiceLanguageCard", VoiceLanguageCard),
    ("VoiceCleanupCard", VoiceCleanupCard),
    ("VoiceMuteCard", VoiceMuteCard),
    ("VoiceHistoryCard", VoiceHistoryCard),
]


def _frame_bg(card) -> str:
    """The card frame's own background declaration (upper-cased for hex compare)."""
    return card.styleSheet().upper()


def main() -> int:
    failures = []
    for name, cls in CARDS:
        card = cls()
        creation = _frame_bg(card)

        card.apply_theme("light")
        light = _frame_bg(card)
        if LIGHT_HEX not in light:
            failures.append(f"{name}: light theme frame bg missing {LIGHT_HEX}\n    got: {light!r}")

        card.apply_theme("dark")
        dark = _frame_bg(card)
        if not any(h in dark for h in DARK_HEXES):
            failures.append(f"{name}: dark theme frame bg missing {DARK_HEXES}\n    got: {dark!r}")

        # Round-trip back to light proves it re-applies, not just a one-way latch.
        card.apply_theme("light")
        if LIGHT_HEX not in _frame_bg(card):
            failures.append(f"{name}: did not return to light on re-toggle")

        status = "FAIL" if any(name in f for f in failures) else "ok"
        print(f"  [{status}] {name}: creation->light->dark->light re-themes correctly")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nAll voice cards re-theme on apply_theme(). PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
