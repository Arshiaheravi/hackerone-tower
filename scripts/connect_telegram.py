"""Confirm an explicitly selected private Telegram destination without sending messages."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from tower.bot import Bot
from tower.config import ConfigError, Settings
from tower.locking import InstanceLock
from tower.store import Store
from tower.telegram import TelegramClient, TelegramError


class SetupError(ValueError):
    """Static, credential-free setup errors suitable for terminal output."""


def _challenge_path(settings: Settings) -> Path:
    return settings.database.parent / "telegram_setup.json"


def _bot_username(telegram) -> str:
    identity = telegram.get_me()
    username = identity.get("username") if isinstance(identity, dict) else None
    if (
        not isinstance(identity, dict)
        or identity.get("is_bot") is not True
        or not isinstance(username, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", username)
    ):
        raise SetupError("Telegram did not return a valid bot username.")
    return username


def _atomic_write(path: Path, content: bytes, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=prefix, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save_destination(env_file: Path, chat_id: int) -> None:
    raw = env_file.read_bytes() if env_file.exists() else b""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SetupError("The environment file must use UTF-8 encoding.") from None
    pattern = re.compile(r"(?m)^(\ufeff?[ \t]*TELEGRAM_ADMIN_CHAT_ID[ \t]*=)[^\r\n]*")
    replacement, count = pattern.subn(lambda match: match[1] + str(chat_id), text)
    if count == 0:
        newline = "\r\n" if "\r\n" in text else "\n"
        separator = "" if not text or text.endswith(("\r", "\n")) else newline
        replacement = text + separator + "TELEGRAM_ADMIN_CHAT_ID=" + str(chat_id) + newline
    # The temporary name is covered by the repository's ignored .env.* pattern.
    _atomic_write(env_file, replacement.encode("utf-8"), ".env.telegram-")


def begin(settings: Settings, telegram, *, clock=time.time) -> dict:
    with InstanceLock(settings.database.with_suffix(".lock")):
        username = _bot_username(telegram)
        challenge = "tower_setup_" + secrets.token_hex(10)
        content = {"challenge": challenge, "created_at": clock()}
        _atomic_write(
            _challenge_path(settings), json.dumps(content).encode("utf-8"), ".telegram-setup-"
        )
    return {
        "setup_url": f"https://t.me/{username}?start={challenge}",
        "bot_username": username,
        "expires_in_seconds": 86400,
    }


def _load_challenge(path: Path, now: float) -> str:
    try:
        content = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SetupError("No valid setup challenge exists. Run begin first.") from None
    if not isinstance(content, dict):
        raise SetupError("No valid setup challenge exists. Run begin first.")
    challenge, created_at = content.get("challenge"), content.get("created_at")
    if isinstance(created_at, str):
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            created_at = parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            created_at = None
    if (
        not isinstance(challenge, str)
        or not re.fullmatch(r"(?:tower_setup_)?[a-f0-9]{20}", challenge)
        or type(created_at) not in (float, int)
        or not math.isfinite(created_at)
        or not 0 <= now - created_at <= 86400
    ):
        raise SetupError("The setup challenge is invalid or expired. Run begin again.")
    return challenge


def _matching_chat(update: dict, challenge: str, username: str) -> int | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender, text = message.get("chat"), message.get("from"), message.get("text")
    if not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    parts = text.split()
    commands = {"/start", f"/start@{username.lower()}"}
    if (
        chat.get("type") != "private"
        or type(chat_id) is not int
        or chat_id <= 0
        or type(sender.get("id")) is not int
        or sender["id"] != chat_id
        or sender.get("is_bot") is not False
        or len(parts) != 2
        or parts[0].lower() not in commands
        or not re.fullmatch(r"(?:tower_setup_)?[a-f0-9]{20}", parts[1])
        or not secrets.compare_digest(parts[1], challenge)
    ):
        return None
    return chat_id


def complete(
    settings: Settings,
    telegram,
    *,
    env_file: Path = Path(".env"),
    wait_seconds: int = 0,
    clock=time.time,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> dict | None:
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise SetupError("Wait duration must be an integer between 0 and 60 seconds.")
    with InstanceLock(settings.database.with_suffix(".lock")), ExitStack() as stack:
        challenge_path = _challenge_path(settings)
        challenge = _load_challenge(challenge_path, clock())
        username = _bot_username(telegram)
        store = Store(settings.database)
        stack.callback(store.close)
        bot = Bot(store)
        deadline = monotonic() + wait_seconds
        first_poll = True
        while first_poll or monotonic() < deadline:
            first_poll = False
            offset = int(store.get_meta("telegram_offset", "0"))
            timeout = min(20, max(0, int(deadline - monotonic())))
            updates = telegram.get_updates(offset, timeout=timeout)
            connected = False
            for update in sorted(updates, key=lambda item: item["update_id"]):
                current_offset = int(store.get_meta("telegram_offset", "0"))
                if type(update.get("update_id")) is not int or update["update_id"] < current_offset:
                    continue
                chat_id = _matching_chat(update, challenge, username)
                if chat_id is not None and not connected:
                    if _load_challenge(challenge_path, clock()) != challenge:
                        raise SetupError("The setup challenge changed. Run begin again.")
                    # Persist consent before acknowledging the update. A failed write
                    # leaves the update available for a safe retry.
                    _save_destination(env_file, chat_id)
                    connected = True
                bot.handle(update)
            if connected:
                # Remove only this exact challenge file; never enumerate or delete a directory.
                challenge_path.unlink()
                return {"connected": True, "destination": "private_chat", "bot_username": username}
            remaining = deadline - monotonic()
            if not updates and remaining > 0:
                sleep(min(0.25, remaining))
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("begin")
    command = commands.add_parser("complete")
    command.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.load(args.env_file)
        if not settings.telegram_token:
            raise SetupError("Set TELEGRAM_BOT_TOKEN in the local environment file first.")
        with TelegramClient(settings.telegram_token) as telegram:
            result = (
                begin(settings, telegram)
                if args.command == "begin"
                else complete(
                    settings, telegram, env_file=args.env_file, wait_seconds=args.wait_seconds
                )
            )
        if result is None:
            print(json.dumps({"connected": False, "status": "waiting_for_start"}))
            return 3
        print(json.dumps(result))
        return 0
    except (SetupError, ConfigError) as exc:
        print(json.dumps({"connected": False, "error": str(exc)}))
        return 2
    except (TelegramError, OSError, RuntimeError, ValueError):
        print(
            json.dumps(
                {
                    "connected": False,
                    "error": "Setup failed. Check bot access and the local worker lock.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
