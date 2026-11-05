"""Safe command-line entry points for local operation and explicit live checks."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import signal
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from tower.bot import BOT_COMMANDS
from tower.config import ConfigError, Settings
from tower.health import health_status as _health
from tower.locking import InstanceLock
from tower.service import Dispatcher, Monitor, Service, configure_logging
from tower.source import HackerOneClient, SourceError
from tower.store import Store
from tower.telegram import TelegramClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tower", description="Public HackerOne program changes and Telegram alerts"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Run the monitor and Telegram workers")
    commands.add_parser("check", help="Check live API access without sending messages")
    commands.add_parser("setup-bot", help="Install and verify the private-chat command menu")
    commands.add_parser(
        "check-source", help="Check HackerOne catalog and one public scope snapshot"
    )
    commands.add_parser(
        "send-test", help="Send one setup message to the configured admin or channel"
    )
    commands.add_parser("status", help="Print local database and freshness status as JSON")
    commands.add_parser("health", help="Print local health JSON; exit nonzero when unhealthy")
    commands.add_parser("demo", help="Exercise the complete workflow offline with fictional data")
    backup = commands.add_parser("backup", help="Back up the database to a new file")
    backup.add_argument("destination", type=Path)
    commands.add_parser(
        "retry-dead", help="Requeue eligible failed deliveries after fixing their cause"
    )
    return parser


def _output(value: dict[str, Any]) -> None:
    if sys.stdout is not None:
        print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))


def _error(message: str) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def _source(settings: Settings) -> HackerOneClient:
    return HackerOneClient(
        settings.username,
        settings.api_token,
        requests_per_minute=settings.requests_per_minute,
        scope_requests_per_minute=settings.scope_requests_per_minute,
    )


def _register(stack: ExitStack, resource: Any) -> Any:
    stack.callback(resource.close)
    return resource


def _run(settings: Settings) -> int:
    settings.require_live()
    configure_logging(settings.log_dir)
    with InstanceLock(settings.database.with_suffix(".lock")), ExitStack() as stack:
        store = _register(stack, Store(settings.database))
        source = _register(stack, _source(settings))
        telegram = _register(stack, TelegramClient(settings.telegram_token))
        service = Service(settings, store, source, telegram)
        old_handlers = {}

        def stop_service(_signum: int, _frame: Any) -> None:
            service.stop.set()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.signal(signum, stop_service)
            service.run()
        finally:
            service.stop.set()
            for signum, previous in old_handlers.items():
                signal.signal(signum, previous)
    return 0


def _check(settings: Settings) -> int:
    settings.require_live()
    with ExitStack() as stack:
        source = _register(stack, _source(settings))
        telegram = _register(stack, TelegramClient(settings.telegram_token))
        programs = source.list_programs()
        identity = telegram.get_me()
    username = identity.get("username")
    _output(
        {
            "api_access_verified": True,
            "visible_public_programs": len(programs),
            "telegram_bot_username": (
                username
                if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", username)
                else "unavailable"
            ),
            "messages_sent": 0,
            "scope_endpoints_checked": False,
        }
    )
    return 0


def _send_test(settings: Settings) -> int:
    if not settings.telegram_token:
        raise ConfigError("Set TELEGRAM_BOT_TOKEN in .env before sending a setup message")
    destination = settings.admin_chat_id or settings.channel_id
    if not destination:
        raise ConfigError("Set TELEGRAM_ADMIN_CHAT_ID or TELEGRAM_CHANNEL_ID for the setup message")
    with ExitStack() as stack:
        telegram = _register(stack, TelegramClient(settings.telegram_token))
        message_id = telegram.send_message(
            destination,
            "HackerOne Tower setup test. Telegram delivery is working. "
            "This is a configuration test, not a program alert. "
            "Check local health for source freshness and worker status.",
        )
    _output({"test_message_sent": True, "message_id": message_id})
    return 0


def _setup_bot(settings: Settings) -> int:
    if not settings.telegram_token:
        raise ConfigError("Set TELEGRAM_BOT_TOKEN before configuring the bot")
    with TelegramClient(settings.telegram_token) as telegram:
        telegram.get_me()
        if telegram.get_webhook_status()["configured"]:
            raise ConfigError(
                "An existing Telegram webhook is configured; resolve it before enabling polling"
            )
        telegram.set_my_commands(BOT_COMMANDS)
        if telegram.get_my_commands() != BOT_COMMANDS:
            raise ConfigError("Telegram command menu verification failed")
    _output(
        {
            "command_menu_verified": True,
            "commands": len(BOT_COMMANDS),
            "webhook_configured": False,
            "messages_sent": 0,
        }
    )
    return 0


def _check_source(settings: Settings) -> int:
    missing = [
        name
        for name, value in (
            ("H1_USERNAME", settings.username),
            ("H1_API_TOKEN", settings.api_token),
        )
        if not value
    ]
    if missing:
        raise ConfigError("Set these values in .env: " + ", ".join(missing))
    with ExitStack() as stack:
        source = _register(stack, _source(settings))
        programs = source.list_programs()
        snapshot = source.fetch_snapshot(programs[0]) if programs else None
    _output(
        {
            "source_access_verified": True,
            "visible_public_programs": len(programs),
            "scope_endpoints_checked": snapshot is not None,
            "sample_public_scope_count": len(snapshot["scopes"]) if snapshot else 0,
            "sample_public_exclusion_count": len(snapshot["exclusions"]) if snapshot else 0,
            "messages_sent": 0,
        }
    )
    return 0


def _local(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.database.is_file():
        _output(
            {
                "healthy": False,
                "error": "database_missing",
                "message": "No existing Tower database. Configure credentials and run the monitor first.",
            }
        )
        return 1
    if args.command == "backup":
        destination = args.destination
        if destination.resolve() == settings.database.resolve() or destination.exists():
            raise ConfigError(
                "Backup destination must be a new file different from the active database"
            )
    with ExitStack() as stack:
        store = _register(stack, Store(settings.database))
        if args.command in {"status", "health"}:
            status = _health(store, settings, time.time())
            _output(status)
            return int(args.command == "health" and not status["healthy"])
        if args.command == "retry-dead":
            _output({"requeued_deliveries": store.retry_dead()})
            return 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Reserve the new path atomically so a concurrent writer cannot be overwritten.
            with destination.open("xb"):
                pass
        except FileExistsError:
            raise ConfigError("Backup destination already exists; choose a new file") from None
        try:
            store.backup(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        _output({"backup_created": True})
    return 0


def _demo() -> int:
    program = {
        "id": "1",
        "handle": "fictional_demo",
        "name": "Fictional demo program (offline fixture)",
        "state": "public_mode",
        "offers_bounties": True,
        "submission_state": "open",
        "policy": "Fictional data for a local demonstration only. No authorization to test any system.",
    }
    snapshot = {"program": program, "scopes": [], "exclusions": []}
    clock = [time.time()]
    sent = []

    class FictionalSource:
        def list_programs(self):
            return [copy.deepcopy(program)]

        def fetch_snapshot(self, _program):
            return copy.deepcopy(snapshot)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.telegram.org" or not request.url.path.endswith("/sendMessage"):
            raise RuntimeError("Unexpected offline demo request")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    with (
        tempfile.TemporaryDirectory(prefix="hackerone-tower-demo-") as directory,
        ExitStack() as stack,
    ):
        store = _register(stack, Store(Path(directory) / "demo.sqlite3"))
        client = stack.enter_context(httpx.Client(transport=httpx.MockTransport(transport)))
        telegram = TelegramClient("123456:offline_demo_token", client)
        settings = Settings()
        monitor = Monitor(store, FictionalSource(), settings, clock=lambda: clock[0])
        dispatcher = Dispatcher(store, telegram, clock=lambda: clock[0])
        store.set_subscription("100000", True, {})
        monitor.tick()
        baseline_count = store.status(clock[0])["event_count"]
        snapshot["scopes"].append(
            {
                "id": "2",
                "asset_identifier": "demo.example.test",
                "asset_type": "URL",
                "eligible_for_bounty": True,
                "eligible_for_submission": True,
                "max_severity": "high",
                "instruction": "Fictional scope entry; this demo makes no requests to this address.",
            }
        )
        clock[0] += 901
        monitor.tick()
        event_count = store.status(clock[0])["event_count"]
        for _ in range(100):
            if not store.pending_deliveries(clock[0]):
                break
            dispatcher.tick()
            clock[0] += 4
        delivered_count = len(sent)
        clock[0] += 901
        monitor.tick()
        dispatcher.tick()
        final = store.status(clock[0])
        if (
            baseline_count != 0
            or event_count != 1
            or final["event_count"] != 1
            or final["pending_deliveries"] != 0
            or delivered_count < 1
            or len(sent) != delivered_count
        ):
            raise RuntimeError("Offline workflow verification failed")
        _output(
            {
                "mode": "offline_demo",
                "passed": True,
                "baseline_events": baseline_count,
                "change_events": event_count,
                "delivered_messages": delivered_count,
                "duplicate_events": final["event_count"] - event_count,
                "network": "MockTransport only; no external requests or messages",
                "sample_alerts": [message["text"] for message in sent],
            }
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # httpx INFO records contain Telegram token-bearing URLs even during `check`.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
    try:
        if args.command == "demo":
            return _demo()
        settings = Settings.load(args.env_file)
        if args.command == "run":
            return _run(settings)
        if args.command == "check":
            return _check(settings)
        if args.command == "check-source":
            return _check_source(settings)
        if args.command == "send-test":
            return _send_test(settings)
        if args.command == "setup-bot":
            return _setup_bot(settings)
        return _local(args, settings)
    except ConfigError as exc:
        _error(str(exc))
        return 2
    except KeyboardInterrupt:
        _error("Stopped by user")
        return 130
    except SourceError as exc:
        code = exc.status_code if exc.status_code is not None else "unavailable"
        category = next(
            (
                label
                for term, label in (
                    ("attribute value", "unsupported_attribute_type"),
                    ("resource type", "unexpected_resource_type"),
                    ("pagination", "pagination_validation"),
                    ("network", "network_connection"),
                    ("scope", "scope_validation"),
                    ("program", "program_validation"),
                )
                if term in str(exc)
            ),
            "source_response",
        )
        _error(f"HackerOne preflight failed (HTTP {code}, {category}).")
        # SourceError messages originate in our adapter, never in remote bodies.
        _error(str(exc))
        return 1
    except Exception:
        message = "Operation failed. Check configuration, connectivity, and local health status; details are redacted."
        logger = logging.getLogger("tower")
        if logger.handlers:
            logger.error(message)
        _error(message)
        return 1
