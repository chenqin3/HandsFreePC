from __future__ import annotations

from typing import Any

# Windows Explorer and some UIA providers expose these invisible formatting
# marks in otherwise ordinary titles, names, and values. They do not identify a
# different window or control, so every Windows text surface removes the same
# deliberately narrow allow-list before comparing or publishing it.
_IGNORABLE_WINDOWS_UI_TEXT = str.maketrans("", "", "\x00\u200b\u200e\u200f")


def sanitize_windows_ui_text(value: Any) -> str:
    """Remove known non-semantic Windows/UIA formatting characters."""

    return "" if value is None else str(value).translate(_IGNORABLE_WINDOWS_UI_TEXT)
