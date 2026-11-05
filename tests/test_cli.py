import json
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tower import cli
from tower.config import Settings
from tower.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    configured = Settings(
        username="example_user",
        api_token="test_api_secret",
        telegram_token="123456:test_bot_secret",
        database=tmp_path / "data" / "tower.sqlite3",
        log_dir=tmp_path / "logs",
    )
    monkeypatch.setattr(cli.Settings, "load", lambda env_file: configured)
    return configured


def test_demo_is_offline_and_exercises_baseline_delivery_and_deduplication(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.Settings, "load", lambda path: pytest.fail("Demo must not load credentials")
    )
    assert cli.main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["baseline_events"] == 0
    assert result["change_events"] == 1
    assert result["duplicate_events"] == 0
    assert result["delivered_messages"] == 1
    assert "demo.example.test" in "".join(result["sample_alerts"])
    assert "NEW ASSET" in result["sample_alerts"][0]
    assert len(result["sample_alerts"][0].encode("utf-16-le")) // 2 <= 800
    assert len(result["sample_alerts"][0].splitlines()) == 5
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", [["status"], ["health"], ["retry-dead"], ["backup", "new.db"]])
def test_local_commands_do_not_create_missing_database(command, settings, capsys):
    assert cli.main(command) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "database_missing"
    assert not settings.database.parent.exists()


def healthy_store(settings):
    store = Store(settings.database)
    now = time.time()
    store.sync_catalog([], now)
    store.set_meta("heartbeat", now)
    for worker in ("catalog", "source", "source_2", "delivery", "bot"):
        if worker != "bot" or settings.personal_bot:
            store.set_meta(f"heartbeat_{worker}", now)
    return store


def test_health_requires_no_credentials(settings, monkeypatch, capsys):
    configured = Settings(database=settings.database)
    monkeypatch.setattr(cli.Settings, "load", lambda env_file: configured)
    store = healthy_store(configured)
    store.close()
    assert cli.main(["health"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is True
    assert result["ready"] is True
    assert result["baseline_complete"] is True
    assert result["reasons"] == []


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("heartbeat", "1", "heartbeat_missing_or_stale"),
        ("last_catalog_at", "1", "catalog_missing_or_stale"),
        ("fatal_worker", "source", "fatal_worker"),
        ("catalog_error", "secret_error_body", "catalog_error"),
        ("scope_error", "source_unavailable", "scope_error"),
        ("bot_error", "telegram_unavailable", "bot_error"),
    ],
)
def test_unhealthy_metadata_sets_exit_code_without_echoing_details(
    key, value, reason, settings, capsys
):
    store = healthy_store(settings)
    store.set_meta(key, value)
    store.close()
    assert cli.main(["health"]) == 1
    output = capsys.readouterr().out
    assert reason in json.loads(output)["reasons"]
    assert "secret_error_body" not in output


def test_status_returns_diagnostics_without_health_exit_gate(settings, capsys):
    Store(settings.database).close()
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is False


def test_dead_delivery_fails_health_and_can_be_requeued(settings, capsys):
    store = healthy_store(settings)
    store.queue_text("42", "setup notification", "test")
    delivery = store.pending_deliveries(time.time())[0]
    store.delivery_failed(delivery["id"], time.time(), permanent=True)
    store.close()
    assert cli.main(["health"]) == 1
    assert "dead_deliveries" in json.loads(capsys.readouterr().out)["reasons"]
    assert cli.main(["retry-dead"]) == 0
    assert json.loads(capsys.readouterr().out)["requeued_deliveries"] == 1
    assert cli.main(["health"]) == 0


def test_backup_preserves_database_content_and_refuses_overwrite(settings, tmp_path, capsys):
    store = healthy_store(settings)
    store.set_meta("fixture_marker", "backup evidence")
    store.close()
    destination = tmp_path / "backups" / "one.sqlite3"
    assert cli.main(["backup", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["backup_created"] is True
    with sqlite3.connect(destination) as database:
        assert (
            database.execute("SELECT value FROM meta WHERE key='fixture_marker'").fetchone()[0]
            == "backup evidence"
        )
    before = destination.read_bytes()
    assert cli.main(["backup", str(destination)]) == 2
    assert destination.read_bytes() == before
    assert cli.main(["backup", str(settings.database)]) == 2


def test_failed_backup_removes_only_its_new_empty_destination(
    settings, tmp_path, monkeypatch, capsys
):
    healthy_store(settings).close()
    destination = tmp_path / "backup.sqlite3"

    def fail_backup(self, path):
        raise RuntimeError("private details must never appear")

    monkeypatch.setattr(Store, "backup", fail_backup)
    assert cli.main(["backup", str(destination)]) == 1
    assert not destination.exists()
    assert settings.database.exists()
    assert "private details" not in capsys.readouterr().err


def test_check_reads_apis_without_creating_database_or_sending_messages(
    settings, monkeypatch, capsys
):
    calls = []

    class Source:
        def __init__(self, username, token, **kwargs):
            assert kwargs["requests_per_minute"] == settings.requests_per_minute
            assert kwargs["scope_requests_per_minute"] == settings.scope_requests_per_minute

        def list_programs(self):
            calls.append("list")
            return [{"id": "1"}, {"id": "2"}]

        def close(self):
            calls.append("source_closed")

    class Telegram:
        def __init__(self, token):
            pass

        def get_me(self):
            calls.append("identity")
            return {"id": 1, "is_bot": True, "username": "example_bot"}

        def close(self):
            calls.append("telegram_closed")

    monkeypatch.setattr(cli, "HackerOneClient", Source)
    monkeypatch.setattr(cli, "TelegramClient", Telegram)
    assert cli.main(["check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["visible_public_programs"] == 2
    assert result["telegram_bot_username"] == "example_bot"
    assert result["messages_sent"] == 0
    assert calls == ["list", "identity", "telegram_closed", "source_closed"]
    assert not settings.database.exists()


@pytest.mark.parametrize(
    "admin,channel,expected",
    [("42", "@news_channel", "42"), ("", "@news_channel", "@news_channel")],
)
def test_send_test_uses_only_explicit_destination(
    admin, channel, expected, settings, monkeypatch, capsys
):
    configured = Settings(telegram_token="123456:fixture", admin_chat_id=admin, channel_id=channel)
    monkeypatch.setattr(cli.Settings, "load", lambda path: configured)
    sent = []
    monkeypatch.setattr(
        cli,
        "TelegramClient",
        lambda token: SimpleNamespace(
            send_message=lambda chat, text: sent.append((chat, text)) or 99,
            close=lambda: None,
        ),
    )
    assert cli.main(["send-test"]) == 0
    assert json.loads(capsys.readouterr().out)["message_id"] == 99
    assert sent[0][0] == expected
    assert "configuration test" in sent[0][1]


def test_send_test_without_destination_fails_without_network(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TelegramClient", lambda token: pytest.fail("No destination"))
    assert cli.main(["send-test"]) == 2
    assert "TELEGRAM_ADMIN_CHAT_ID" in capsys.readouterr().err


def test_run_stops_on_signal_restores_handlers_and_then_closes_resources(settings, monkeypatch):
    calls = []
    old_int, old_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "configure_logging", lambda directory: calls.append("logging"))
    monkeypatch.setattr(
        cli, "Store", lambda path: SimpleNamespace(close=lambda: calls.append("store_closed"))
    )
    monkeypatch.setattr(
        cli,
        "_source",
        lambda configured: SimpleNamespace(close=lambda: calls.append("source_closed")),
    )
    monkeypatch.setattr(
        cli,
        "TelegramClient",
        lambda token: SimpleNamespace(close=lambda: calls.append("telegram_closed")),
    )

    class Service:
        def __init__(self, *_):
            self.stop = threading.Event()

        def run(self):
            calls.append("run")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert self.stop.is_set()
            calls.append("stopped")

    monkeypatch.setattr(cli, "Service", Service)
    assert cli.main(["run"]) == 0
    assert calls == [
        "logging",
        "run",
        "stopped",
        "telegram_closed",
        "source_closed",
        "store_closed",
    ]
    assert signal.getsignal(signal.SIGINT) == old_int
    assert signal.getsignal(signal.SIGTERM) == old_term


def test_live_command_requires_credentials_before_creating_files(tmp_path, monkeypatch, capsys):
    configured = Settings(database=tmp_path / "data.db", log_dir=tmp_path / "logs")
    monkeypatch.setattr(cli.Settings, "load", lambda path: configured)
    assert cli.main(["run"]) == 2
    assert "H1_API_TOKEN" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_arbitrary_failure_is_redacted(settings, monkeypatch, capsys):
    secret = "secret_token_that_must_not_appear"

    def fail_source(configured):
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_source", fail_source)
    assert cli.main(["check"]) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "Traceback" not in output.err
    assert "details are redacted" in output.err


def test_source_error_shows_safe_status_without_credentials(settings, monkeypatch, capsys):
    from tower.source import SourceError

    def fail_source(configured):
        raise SourceError("HackerOne request rejected", status_code=401)

    monkeypatch.setattr(cli, "_source", fail_source)
    assert cli.main(["check-source"]) == 1
    output = capsys.readouterr()
    assert "HTTP 401" in output.err
    assert "Traceback" not in output.err


def test_setup_bot_verifies_private_menu_without_sending_messages(settings, monkeypatch, capsys):
    import httpx

    from tower.telegram import TelegramClient

    commands = []
    methods = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "username": "fixture_bot"}
        elif method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "setMyCommands":
            commands.extend(json.loads(request.content)["commands"])
            result = True
        else:
            assert method == "getMyCommands"
            result = commands
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(cli, "TelegramClient", lambda token: TelegramClient(token, client))
        assert cli.main(["setup-bot"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command_menu_verified"] is True
    assert result["commands"] == 16
    assert {"all", "guide", "menu"} <= {command["command"] for command in commands}
    assert result["messages_sent"] == 0
    assert "sendMessage" not in methods


def test_setup_bot_preserves_existing_webhook_without_exposing_its_url(
    settings, monkeypatch, capsys
):
    import httpx

    from tower.telegram import TelegramClient

    def handler(request):
        if request.url.path.endswith("getMe"):
            result = {"id": 123, "is_bot": True}
        else:
            assert request.url.path.endswith("getWebhookInfo")
            result = {"url": "https://example.invalid/sensitive-integration-secret"}
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(cli, "TelegramClient", lambda token: TelegramClient(token, client))
        assert cli.main(["setup-bot"]) == 2
    output = capsys.readouterr()
    assert "existing Telegram webhook" in output.err
    assert "sensitive-integration-secret" not in output.err + output.out


def test_check_source_needs_no_telegram_and_prints_only_counts(settings, monkeypatch, capsys):
    configured = Settings(username="fixture_user", api_token="fixture_secret")
    monkeypatch.setattr(cli.Settings, "load", lambda path: configured)
    calls = []
    monkeypatch.setattr(
        cli, "TelegramClient", lambda token: pytest.fail("Source check must not use Telegram")
    )
    monkeypatch.setattr(
        cli,
        "_source",
        lambda settings: SimpleNamespace(
            list_programs=lambda: [{"id": "1", "name": "do_not_print_program_name"}],
            fetch_snapshot=lambda program: calls.append(program["id"])
            or {"scopes": [{"id": "2"}, {"id": "3"}], "exclusions": [{"id": "4"}]},
            close=lambda: calls.append("closed"),
        ),
    )
    assert cli.main(["check-source"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["visible_public_programs"] == 1
    assert result["sample_public_scope_count"] == 2
    assert result["sample_public_exclusion_count"] == 1
    assert result["scope_endpoints_checked"] is True
    assert result["messages_sent"] == 0
    assert calls == ["1", "closed"]
    assert "do_not_print_program_name" not in output


def test_check_source_handles_empty_public_catalog(settings, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_source",
        lambda settings: SimpleNamespace(
            list_programs=lambda: [],
            fetch_snapshot=lambda program: pytest.fail("No public program available"),
            close=lambda: None,
        ),
    )
    assert cli.main(["check-source"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["visible_public_programs"] == 0
    assert result["scope_endpoints_checked"] is False


def test_check_source_rejects_missing_credentials_before_network(monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "load", lambda path: Settings())
    monkeypatch.setattr(cli, "_source", lambda settings: pytest.fail("Missing credentials"))
    assert cli.main(["check-source"]) == 2
    assert "H1_API_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, "not a date", "nan", "inf", str(time.time() + 10000)])
def test_invalid_heartbeat_is_unhealthy(value, settings, capsys):
    store = healthy_store(settings)
    store.set_meta("heartbeat", value)
    store.close()
    assert cli.main(["health"]) == 1
    assert "heartbeat_missing_or_stale" in json.loads(capsys.readouterr().out)["reasons"]


@pytest.mark.parametrize("worker", ["catalog", "source", "source_2", "delivery", "bot"])
@pytest.mark.parametrize("missing", [False, True])
def test_health_rejects_missing_or_stalled_worker_with_live_main_heartbeat(
    settings, worker, missing
):
    store = healthy_store(settings)
    now = time.time()
    thresholds = {
        "catalog": max(600, settings.catalog_interval * 3),
        "source": max(600, settings.scope_refresh * 2),
        "source_2": max(600, settings.scope_refresh * 2),
        "delivery": 120,
        "bot": 120,
    }
    store.set_meta(f"heartbeat_{worker}", "" if missing else now - thresholds[worker] - 1)
    try:
        result = cli._health(store, settings, now)
        assert result["healthy"] is False
        assert result["ready"] is False
        assert result["reasons"] == [f"worker_{worker}_missing_or_stale"]
    finally:
        store.close()


def test_disabled_personal_bot_does_not_require_bot_heartbeat(settings):
    configured = Settings(database=settings.database, personal_bot=False)
    store = healthy_store(configured)
    try:
        result = cli._health(store, configured, time.time())
        assert result["healthy"] is True
        assert "bot" not in result["worker_heartbeat_age_seconds"]
    finally:
        store.close()


def test_baseline_in_progress_is_healthy_but_not_ready(settings):
    store = healthy_store(settings)
    now = time.time()
    program = {"id": "1", "handle": "example", "name": "Example", "state": "public_mode"}
    store.sync_catalog([program], now)
    try:
        result = cli._health(store, settings, now)
        assert result["healthy"] is True and result["reasons"] == []
        assert result["ready"] is False and result["baseline_complete"] is False
        assert result["scope_progress_age_seconds"] is None
        store.apply_snapshot({"program": program, "scopes": [], "exclusions": []}, now, 900)
        result = cli._health(store, settings, now)
        assert result["healthy"] is True and result["ready"] is True
    finally:
        store.close()


def test_scope_progress_can_be_stale_even_when_all_workers_have_recent_heartbeats(settings):
    store = healthy_store(settings)
    now = time.time()
    program = {"id": "1", "handle": "example", "name": "Example", "state": "public_mode"}
    store.sync_catalog([program], now)
    store.apply_snapshot({"program": program, "scopes": [], "exclusions": []}, now, 900)
    store.set_meta("last_scope_at", now - max(600, settings.scope_refresh * 2) - 1)
    try:
        result = cli._health(store, settings, now)
        assert result["reasons"] == ["scope_progress_stale"]
        assert result["healthy"] is False and result["ready"] is False
        store.sync_catalog([], now)
        assert "scope_progress_stale" not in cli._health(store, settings, now)["reasons"]
    finally:
        store.close()


def test_stale_pending_delivery_fails_health_and_recovers_when_delivered(settings, monkeypatch):
    store = healthy_store(settings)
    now = time.time()
    old = now - max(300, settings.scope_refresh) - 1
    with monkeypatch.context() as patch:
        patch.setattr("tower.store.time.time", lambda: old)
        store.queue_text("123", "waiting reply", "fixture")
    try:
        result = cli._health(store, settings, now)
        assert result["reasons"] == ["pending_delivery_stale"]
        assert result["pending_delivery_age_seconds"] == now - old
        delivery = store.pending_deliveries(now)[0]
        store.delivered(delivery["id"], 1, now)
        result = cli._health(store, settings, now)
        assert result["healthy"] is True and result["ready"] is True
        assert result["pending_delivery_age_seconds"] is None
    finally:
        store.close()
