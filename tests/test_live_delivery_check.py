import json

import httpx
import pytest

from scripts import live_delivery_check as check
from tower.config import ConfigError, Settings
from tower.telegram import TelegramClient


class Clock:
    def __init__(self):
        self.now = 1_793_661_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def settings(tmp_path):
    return Settings(
        telegram_token="123456:fixture_token_only",
        admin_chat_id="777000",
        channel_id="@fictional_channel",
        database=tmp_path / "production.sqlite3",
    )


def run(settings, handler, **kwargs):
    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        telegram = TelegramClient(settings.telegram_token, http)
        return check.run_check(
            settings,
            telegram,
            clock=clock,
            monotonic=clock,
            sleep=clock.sleep,
            **kwargs,
        )


def test_acceptance_uses_real_store_restart_and_dispatcher_without_production_writes(
    settings, monkeypatch
):
    lifecycles = []
    actual_store = check.Store

    class TrackedStore(actual_store):
        def __init__(self, path):
            super().__init__(path)
            lifecycles.append("open")

        def close(self):
            super().close()
            lifecycles.append("close")

    monkeypatch.setattr(check, "Store", TrackedStore)
    requests = []

    def handler(request):
        assert request.url.host == "api.telegram.org"
        assert request.url.path.endswith("/sendMessage")
        assert lifecycles == ["open", "close", "open"]
        body = json.loads(request.content)
        requests.append(body)
        assert body["chat_id"] == settings.admin_chat_id
        assert "SETUP TEST — fictional program" in body["text"]
        assert "SETUP TEST ONLY" not in body["text"]  # Long fixture instructions stay local.
        assert "[out of scope]" in body["text"]
        assert "[no bounty]" in body["text"]
        assert "setup-test.example.test" in body["text"]
        assert "NEW ASSET" in body["text"]
        assert "OBSERVED RECORD" not in body["text"]
        assert len(body["text"].encode("utf-16-le")) // 2 <= 800
        assert len(body["text"].splitlines()) == 5
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 901}})

    result = run(settings, handler)
    assert result["telegram_api_acceptance_verified"] is True
    assert result["human_receipt_verified"] is False
    assert result["baseline_events"] == 0
    assert result["event_count"] == 1
    assert result["duplicate_events"] == 0
    assert result["restart_verified"] is True
    assert result["message_ids"] == [901]
    assert result["send_attempts"] == result["delivered_messages"] == 1
    assert result["failed_attempts"] == 0
    assert len(requests) == 1
    assert lifecycles == ["open", "close", "open", "close"]
    assert not settings.database.exists()
    encoded = json.dumps(result)
    assert settings.telegram_token not in encoded
    assert settings.admin_chat_id not in encoded
    assert settings.channel_id not in encoded


def test_channel_is_used_only_when_admin_not_configured():
    settings = Settings(telegram_token="123456:fixture", channel_id="@configured_channel")

    def handler(request):
        assert json.loads(request.content)["chat_id"] == "@configured_channel"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 12}})

    assert run(settings, handler)["telegram_api_acceptance_verified"] is True


@pytest.mark.parametrize(
    "settings",
    [Settings(telegram_token="123456:fixture"), Settings(admin_chat_id="777000")],
)
def test_missing_explicit_destination_or_token_never_constructs_a_client(settings, monkeypatch):
    monkeypatch.setattr(
        check, "TelegramClient", lambda token: pytest.fail("No authorized destination")
    )
    with pytest.raises(ConfigError):
        check.run_check(settings)


def test_permanent_rejection_is_persisted_and_stops_after_one_attempt(settings):
    result = run(
        settings,
        lambda request: httpx.Response(403, json={"ok": False, "description": "private response"}),
    )
    assert result["telegram_api_acceptance_verified"] is False
    assert result["dead_messages"] == 1
    assert result["failed_attempts"] == result["send_attempts"] == 1
    assert result["message_ids"] == []
    assert "private response" not in json.dumps(result)


def test_ambiguous_network_failures_are_bounded_to_three_attempts(settings):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private request content", request=request)

    result = run(settings, handler)
    assert len(calls) == 3
    assert result["send_attempts"] == result["failed_attempts"] == 3
    assert result["telegram_api_acceptance_verified"] is False
    assert result["duplicate_delivery_possible"] is True
    assert result["pending_messages"] == 1


def test_rate_limit_then_success_preserves_failed_attempt_count(settings):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 5}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 333}})

    result = run(settings, handler)
    assert result["telegram_api_acceptance_verified"] is True
    assert result["send_attempts"] == 2
    assert result["failed_attempts"] == 1
    assert result["message_ids"] == [333]


def test_long_retry_after_is_not_ignored_or_waited_forever(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 120}})

    result = run(settings, handler)
    assert len(calls) == 1
    assert result["telegram_api_acceptance_verified"] is False
    assert result["pending_messages"] == 1


@pytest.mark.parametrize("seconds", [0, -1, 31, float("inf"), float("nan")])
def test_deadline_cannot_be_extended_beyond_thirty_seconds(settings, seconds, monkeypatch):
    monkeypatch.setattr(check, "TelegramClient", lambda token: pytest.fail("Invalid deadline"))
    with pytest.raises(ValueError):
        check.run_check(settings, deadline_seconds=seconds)


def test_cli_outputs_safe_failure_json_without_private_exception(settings, monkeypatch, capsys):
    monkeypatch.setattr(check.Settings, "load", lambda path: settings)

    def fail(configured):
        raise RuntimeError("private_token_and_chat_id")

    monkeypatch.setattr(check, "run_check", fail)
    assert check.main([]) == 1
    output = capsys.readouterr().out
    assert "private_token_and_chat_id" not in output
    assert json.loads(output)["telegram_api_acceptance_verified"] is False


def test_cli_success_and_missing_config_exit_codes(settings, monkeypatch, capsys):
    monkeypatch.setattr(check.Settings, "load", lambda path: settings)
    monkeypatch.setattr(
        check, "run_check", lambda configured: {"telegram_api_acceptance_verified": True}
    )
    assert check.main([]) == 0
    assert json.loads(capsys.readouterr().out)["telegram_api_acceptance_verified"] is True

    def missing(configured):
        raise ConfigError("private input must not be echoed")

    monkeypatch.setattr(check, "run_check", missing)
    assert check.main([]) == 2
    output = capsys.readouterr().out
    assert "private input" not in output
    assert "explicit TELEGRAM_ADMIN_CHAT_ID" in json.loads(output)["error"]
