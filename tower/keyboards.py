"""Bounded text-only Telegram menus; no personal-data or web-app requests.

Telegram supports text strings as a shorthand for simple KeyboardButton objects:
https://core.telegram.org/bots/api#replykeyboardmarkup
"""

from __future__ import annotations

import unicodedata
from typing import Any

MAIN_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        ["Recent changes", "Monitoring status"],
        ["My settings", "Quick guide"],
        ["Watch everything", "Bounty programs"],
        ["Pause alerts", "Resume alerts"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Choose an option or type /help",
}

BUTTON_COMMANDS = {
    "Recent changes": "/recent",
    "Monitoring status": "/status",
    "My settings": "/settings",
    "Quick guide": "/guide",
    "Watch everything": "/all",
    "Bounty programs": "/bounties on",
    "Pause alerts": "/pause",
    "Resume alerts": "/resume",
}

_BOOLEAN_FIELDS = frozenset({"resize_keyboard", "is_persistent", "one_time_keyboard", "selective"})
_ALLOWED_FIELDS = _BOOLEAN_FIELDS | {"keyboard", "input_field_placeholder"}
_ERROR = "Invalid Telegram reply keyboard"


def _text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.strip()
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)
        or len(value.encode("utf-16-le")) // 2 > 64
    ):
        raise ValueError(_ERROR)
    return value


def validate_reply_markup(value: Any) -> dict[str, Any] | None:
    """Return an independent normalized menu, or None for an ordinary message.

    Menus are limited to eight rows, eight buttons per row, 32 buttons total,
    and 64 UTF-16 units per label/placeholder. Text-only button objects are
    normalized to string shorthand. All other button capabilities are rejected.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - _ALLOWED_FIELDS:
        raise ValueError(_ERROR)
    rows = value.get("keyboard")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError(_ERROR)
    normalized_rows: list[list[str]] = []
    total_buttons = 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 8:
            raise ValueError(_ERROR)
        total_buttons += len(row)
        if total_buttons > 32:
            raise ValueError(_ERROR)
        normalized_row = []
        for button in row:
            if isinstance(button, dict):
                if set(button) != {"text"}:
                    raise ValueError(_ERROR)
                button = button["text"]
            normalized_row.append(_text(button))
        normalized_rows.append(normalized_row)
    normalized: dict[str, Any] = {"keyboard": normalized_rows}
    for field in _BOOLEAN_FIELDS & value.keys():
        if type(value[field]) is not bool:
            raise ValueError(_ERROR)
        normalized[field] = value[field]
    if "input_field_placeholder" in value:
        normalized["input_field_placeholder"] = _text(value["input_field_placeholder"])
    return normalized
