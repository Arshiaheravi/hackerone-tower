"""Small Telegram Bot API adapter with safe errors and explicit retry ownership."""

from __future__ import annotations

import math
import re
from typing import Any

import httpx

from tower.keyboards import validate_reply_markup


class TelegramError(Exception):
    """An API failure safe to log without leaking a token or response body."""

    def __init__(self, *, retry_after: float | None = None, permanent: bool = False) -> None:
        super().__init__("Telegram request failed")
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramClient:
    """Send once per call; the durable outbox owns delivery retries.

    A timeout can mean Telegram accepted the message but its reply was lost.
    There is no Bot API idempotency key, so callers must allow duplicate delivery.
    """

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Telegram bot token has an invalid format")
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(20, connect=10),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, payload: dict[str, Any], *, timeout: int = 0) -> Any:
        try:
            response = self._client.post(
                self._base_url + method,
                json=payload,
                follow_redirects=False,
                timeout=httpx.Timeout(max(20, timeout + 10), connect=10),
            )
        except httpx.HTTPError:
            raise TelegramError() from None

        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = None

        if not isinstance(body, dict) or body.get("ok") is not True or not response.is_success:
            code = response.status_code
            if response.is_success and isinstance(body, dict):
                candidate = body.get("error_code")
                if type(candidate) is int:
                    code = candidate
            retry_after = None
            if code == 429 and isinstance(body, dict):
                parameters = body.get("parameters")
                if isinstance(parameters, dict):
                    candidate = parameters.get("retry_after")
                    if (
                        type(candidate) in (int, float)
                        and math.isfinite(candidate)
                        and candidate >= 0
                    ):
                        retry_after = float(candidate)
            raise TelegramError(
                retry_after=retry_after, permanent=code in (400, 401, 403)
            ) from None
        if "result" not in body:
            raise TelegramError()
        return body["result"]

    def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> int:
        if not str(chat_id) or not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Telegram message requires a destination and 1-4096 UTF-16 units")
        markup = validate_reply_markup(reply_markup)
        if markup is not None and not re.fullmatch(r"[1-9][0-9]*", str(chat_id)):
            raise ValueError("Reply keyboards require a positive private numeric Telegram chat")
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if markup is not None:
            payload["reply_markup"] = markup
        result = self._request(
            "sendMessage",
            payload,
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError()
        return result["message_id"]

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        if (
            type(offset) is not int
            or offset < 0
            or type(timeout) is not int
            or not 0 <= timeout <= 50
        ):
            raise ValueError("Telegram update offset or timeout is invalid")
        result = self._request(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": 100, "allowed_updates": ["message"]},
            timeout=timeout,
        )
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or type(item.get("update_id")) is not int for item in result
        ):
            raise TelegramError()
        return result

    def get_me(self) -> dict[str, Any]:
        result = self._request("getMe", {})
        if (
            not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result.get("is_bot") is not True
        ):
            raise TelegramError()
        return result

    def get_webhook_status(self) -> dict[str, Any]:
        result = self._request("getWebhookInfo", {})
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise TelegramError()
        # Never expose the webhook URL: it may contain another integration's secret.
        return {"configured": bool(result["url"])}

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self._validate_commands(commands)
        result = self._request(
            "setMyCommands",
            {
                "commands": commands,
                "scope": {"type": "all_private_chats"},
            },
        )
        if result is not True:
            raise TelegramError()

    def get_my_commands(self) -> list[dict[str, str]]:
        result = self._request("getMyCommands", {"scope": {"type": "all_private_chats"}})
        try:
            self._validate_commands(result)
        except ValueError:
            raise TelegramError() from None
        return result

    @staticmethod
    def _validate_commands(commands) -> None:
        if not isinstance(commands, list) or len(commands) > 100:
            raise ValueError("Invalid Telegram commands")
        for command in commands:
            if (
                not isinstance(command, dict)
                or set(command) != {"command", "description"}
                or not isinstance(command["command"], str)
                or not re.fullmatch(r"[a-z0-9_]{1,32}", command["command"])
                or not isinstance(command["description"], str)
                or not 1 <= len(command["description"]) <= 256
            ):
                raise ValueError("Invalid Telegram commands")
