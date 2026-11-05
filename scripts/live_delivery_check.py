"""Explicit Telegram acceptance check using a temporary fictional change journal.

Run only when a test message to the configured admin/channel is intended:
    python -m scripts.live_delivery_check

This does not touch the production database or contact HackerOne or listed assets.
The polling deadline is 30 seconds; an HTTP request already in flight retains the
Telegram adapter's bounded timeout. At most three send attempts are permitted.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from tower.config import ConfigError, Settings
from tower.service import Dispatcher
from tower.store import Store
from tower.telegram import TelegramClient, TelegramError


def _delivery_summary(path: Path) -> dict[str, Any]:
    """Read only non-sensitive acceptance evidence, including persisted failures."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT state,attempts,message_id FROM deliveries ORDER BY id"
        ).fetchall()
    return {
        "queued_messages": len(rows),
        "delivered_messages": sum(row[0] == "delivered" for row in rows),
        "pending_messages": sum(row[0] == "pending" for row in rows),
        "dead_messages": sum(row[0] == "dead" for row in rows),
        "failed_attempts": sum(row[1] for row in rows),
        "message_ids": [row[2] for row in rows if row[0] == "delivered"],
    }


class _BoundedSender:
    def __init__(self, telegram: TelegramClient):
        self.telegram = telegram
        self.attempts = 0

    def send_message(self, chat_id: str, text: str) -> int:
        if self.attempts >= 3:
            raise TelegramError(permanent=True)
        self.attempts += 1
        return self.telegram.send_message(chat_id, text)


def run_check(
    settings: Settings,
    telegram: TelegramClient | None = None,
    *,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_seconds: float = 30.0,
) -> dict[str, Any]:
    """Send one clearly marked fixture alert, with bounded retry attempts.

    Only a configured admin/channel can receive the test. API acknowledgement
    proves Telegram accepted a request; it does not prove a human read it.
    """
    destination = settings.admin_chat_id or settings.channel_id
    if not settings.telegram_token or not destination:
        raise ConfigError("Configure a Telegram token and an explicit admin or channel destination")
    if not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 30:
        raise ValueError("Live check deadline must be greater than zero and at most 30 seconds")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True

    program = {
        "id": "1",
        "handle": "fictional_setup_test",
        "name": "SETUP TEST — fictional program",
        "state": "public_mode",
        "policy": "SETUP TEST ONLY. Fictional metadata, not a real program or testing authorization.",
    }
    baseline = {"program": program, "scopes": [], "exclusions": []}
    changed = {
        "program": program,
        "scopes": [
            {
                "id": "2",
                "asset_identifier": "setup-test.example.test",
                "asset_type": "URL",
                "eligible_for_submission": False,
                "eligible_for_bounty": False,
                "instruction": "SETUP TEST ONLY. Fictional scope entry; do not test this address.",
            }
        ],
        "exclusions": [],
    }
    with tempfile.TemporaryDirectory(prefix="tower-live-delivery-check-") as directory:
        database = Path(directory) / "acceptance.sqlite3"
        with closing(Store(database)) as store:
            store.set_subscription(destination, True, {})
            store.sync_catalog([program], clock())
            initial = store.apply_snapshot(baseline, clock(), 900)
            events = store.apply_snapshot(changed, clock(), 900)
            queued = _delivery_summary(database)
            if initial or len(events) != 1 or queued["queued_messages"] != 1:
                raise RuntimeError("Unexpected live check fixture size")

        # Close and reopen the actual SQLite journal before the first send.
        with closing(Store(database)) as store:
            persisted = _delivery_summary(database)
            restart_verified = persisted["pending_messages"] == 1
            repeated = store.apply_snapshot(changed, clock(), 900)
            if not restart_verified or repeated:
                raise RuntimeError("Live check persistence verification failed")
            owns_telegram = telegram is None
            active_telegram = telegram or TelegramClient(settings.telegram_token)
            sender = _BoundedSender(active_telegram)
            try:
                dispatcher = Dispatcher(store, sender, settings.catalog_interval, clock=clock)
                deadline = monotonic() + deadline_seconds
                while monotonic() < deadline and sender.attempts < 3:
                    dispatcher.tick()
                    evidence = _delivery_summary(database)
                    if evidence["pending_messages"] == 0 or evidence["dead_messages"]:
                        break
                    sleep(min(0.2, max(0.0, deadline - monotonic())))
                evidence = _delivery_summary(database)
                final_event_count = store.status(clock())["event_count"]
                accepted = (
                    restart_verified
                    and final_event_count == 1
                    and evidence["delivered_messages"] == evidence["queued_messages"] == 1
                    and evidence["pending_messages"] == 0
                    and evidence["dead_messages"] == 0
                )
                return {
                    "telegram_api_acceptance_verified": accepted,
                    "human_receipt_verified": False,
                    "baseline_events": len(initial),
                    "event_count": final_event_count,
                    "duplicate_events": len(repeated),
                    "restart_verified": restart_verified,
                    "send_attempts": sender.attempts,
                    "duplicate_delivery_possible": evidence["failed_attempts"] > 0,
                    **evidence,
                }
            finally:
                if owns_telegram:
                    active_telegram.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a labelled fictional Telegram acceptance test"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        result = run_check(Settings.load(args.env_file))
        code = 0 if result["telegram_api_acceptance_verified"] else 1
    except ConfigError:
        result = {
            "telegram_api_acceptance_verified": False,
            "error": "Configure TELEGRAM_BOT_TOKEN and an explicit TELEGRAM_ADMIN_CHAT_ID or TELEGRAM_CHANNEL_ID.",
        }
        code = 2
    except Exception:
        result = {
            "telegram_api_acceptance_verified": False,
            "error": "Live delivery check failed; details are redacted.",
        }
        code = 1
    if sys.stdout is not None:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
