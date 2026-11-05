"""Explicit, non-interpolating configuration; secrets never enter repr or logs."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    pass


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid environment assignment on line {number}")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ConfigError(f"Invalid environment key on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class Settings:
    username: str = ""
    api_token: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    channel_id: str = ""
    admin_chat_id: str = ""
    database: Path = Path("data/tower.sqlite3")
    log_dir: Path = Path("logs")
    catalog_interval: int = 120
    scope_refresh: int = 900
    scope_workers: int = 2
    requests_per_minute: int = 300
    scope_requests_per_minute: int = 40
    retention_days: int = 90
    personal_bot: bool = True

    @classmethod
    def load(cls, env_file: Path = Path(".env"), environ: Mapping[str, str] | None = None):
        values = read_env(env_file)
        values.update(os.environ if environ is None else environ)

        def number(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                result = int(values.get(key, str(default)))
            except ValueError:
                raise ConfigError(f"{key} must be an integer") from None
            if not minimum <= result <= maximum:
                raise ConfigError(f"{key} must be between {minimum} and {maximum}")
            return result

        boolean = values.get("ENABLE_PERSONAL_BOT", "true").lower()
        if boolean not in {"true", "false"}:
            raise ConfigError("ENABLE_PERSONAL_BOT must be true or false")
        channel = values.get("TELEGRAM_CHANNEL_ID", "")
        admin = values.get("TELEGRAM_ADMIN_CHAT_ID", "")
        if channel and not re.fullmatch(r"(?:-?\d+|@[A-Za-z][A-Za-z0-9_]{4,})", channel):
            raise ConfigError("TELEGRAM_CHANNEL_ID must be a numeric ID or @channel username")
        if admin and not re.fullmatch(r"[1-9]\d*", admin):
            raise ConfigError("TELEGRAM_ADMIN_CHAT_ID must be a positive private chat ID")
        return cls(
            username=values.get("H1_USERNAME", ""),
            api_token=values.get("H1_API_TOKEN", ""),
            telegram_token=values.get("TELEGRAM_BOT_TOKEN", ""),
            channel_id=channel,
            admin_chat_id=admin,
            database=Path(values.get("TOWER_DATABASE", "data/tower.sqlite3")),
            log_dir=Path(values.get("TOWER_LOG_DIR", "logs")),
            catalog_interval=number("CATALOG_INTERVAL_SECONDS", 120, 60, 86400),
            scope_refresh=number("SCOPE_REFRESH_SECONDS", 900, 60, 86400),
            scope_workers=number("SCOPE_WORKERS", 2, 1, 2),
            requests_per_minute=number("H1_REQUESTS_PER_MINUTE", 300, 1, 500),
            scope_requests_per_minute=number("H1_SCOPE_REQUESTS_PER_MINUTE", 40, 1, 40),
            retention_days=number("RETENTION_DAYS", 90, 7, 3650),
            personal_bot=boolean == "true",
        )

    def require_live(self):
        missing = [
            name
            for name, value in (
                ("H1_USERNAME", self.username),
                ("H1_API_TOKEN", self.api_token),
                ("TELEGRAM_BOT_TOKEN", self.telegram_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError("Set these values in .env: " + ", ".join(missing))
        if not self.personal_bot and not self.channel_id:
            raise ConfigError("Set a Telegram channel or enable personal bot subscriptions")
