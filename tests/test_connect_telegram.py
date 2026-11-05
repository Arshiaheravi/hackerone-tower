"""Onboarding uses temporary fake credentials and never sends messages."""

import json
import time

import pytest

from scripts import connect_telegram as setup
from tower.config import Settings
from tower.locking import InstanceLock
from tower.store import Store

CHALLENGE = "tower_setup_0123456789abcdef0123"


class FakeTelegram:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.polls = []
        self.identity = {"id": 777, "is_bot": True, "username": "ExampleTowerBot"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def get_me(self):
        return self.identity

    def get_updates(self, offset, timeout=0):
        self.polls.append((offset, timeout))
        return self.batches.pop(0) if self.batches else []

    def send_message(self, *_):
        raise AssertionError("Onboarding must never send messages")


def message(number=1, chat_id=123456789, text=None):
    return {
        "update_id": number,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False},
            "text": text or f"/start {CHALLENGE}",
        },
    }


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        database=tmp_path / "data" / "tower.sqlite3", telegram_token="123:fake-only"
    )
    settings.database.parent.mkdir()
    challenge_path = settings.database.parent / "telegram_setup.json"
    challenge_path.write_text(json.dumps({"challenge": CHALLENGE, "created_at": 1000}))
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b"\xef\xbb\xbfH1_API_TOKEN=fake-value\r\nTELEGRAM_ADMIN_CHAT_ID=old\r\n# Preserve\r\nOTHER=literal=value"
    )
    return settings, challenge_path, env_file


def complete(context, telegram, **kwargs):
    settings, _, env_file = context
    return setup.complete(settings, telegram, env_file=env_file, clock=lambda: 1001, **kwargs)


def test_begin_generates_prefixed_expiring_challenge_and_verified_link(context):
    settings, path, _ = context
    result = setup.begin(settings, FakeTelegram(), clock=lambda: 2000)
    saved = json.loads(path.read_text())
    assert saved["challenge"].startswith("tower_setup_")
    assert len(saved["challenge"].removeprefix("tower_setup_")) == 20
    assert saved["created_at"] == 2000
    assert result["setup_url"] == f"https://t.me/ExampleTowerBot?start={saved['challenge']}"
    assert settings.telegram_token not in json.dumps(result)


def test_existing_prefixed_challenge_processes_all_updates_preserving_config(context):
    settings, path, env_file = context
    old = env_file.read_bytes()
    telegram = FakeTelegram([[message(3, 200, "/pause"), message(1, 200, "/start"), message(2)]])
    result = complete(context, telegram)
    assert result == {
        "connected": True,
        "destination": "private_chat",
        "bot_username": "ExampleTowerBot",
    }
    assert "123456789" not in json.dumps(result)
    assert not path.exists()
    assert env_file.read_bytes() == old.replace(
        b"TELEGRAM_ADMIN_CHAT_ID=old", b"TELEGRAM_ADMIN_CHAT_ID=123456789"
    )
    assert list(env_file.parent.glob(".env.telegram-*")) == []
    store = Store(settings.database)
    try:
        assert store.get_meta("telegram_offset") == "4"
        assert store.get_subscription("200")["active"] is False
        assert store.get_subscription("123456789")["active"] is True
        assert store.status(1001)["pending_deliveries"] == 3
    finally:
        store.close()
    with pytest.raises(setup.SetupError, match="begin"):
        complete(context, telegram)
    assert len(telegram.polls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["message"]["chat"].update(type="group"),
        lambda item: item["message"]["from"].update(id=999),
        lambda item: item["message"]["from"].update(is_bot=True),
        lambda item: item["message"]["from"].pop("is_bot"),
        lambda item: item["message"]["chat"].update(id=-1),
        lambda item: item["message"].update(text="/start " + "f" * 20),
        lambda item: item["message"].update(text="/start \U0001f916"),
        lambda item: item["message"].update(text=f"/start@OtherBot {CHALLENGE}"),
        lambda item: item["message"].update(text=f"/start {CHALLENGE} extra"),
    ],
)
def test_connection_requires_same_nonbot_private_sender_and_exact_challenge(context, mutation):
    _, path, env_file = context
    old = env_file.read_bytes()
    item = message()
    mutation(item)
    assert complete(context, FakeTelegram([[item]])) is None
    assert env_file.read_bytes() == old and path.exists()


@pytest.mark.parametrize("created_at", [-90000, 1002, None, True, "invalid"])
def test_expired_future_or_invalid_challenge_is_rejected_before_polling(context, created_at):
    _, path, _ = context
    path.write_text(json.dumps({"challenge": CHALLENGE, "created_at": created_at}))
    telegram = FakeTelegram()
    with pytest.raises(setup.SetupError, match="invalid or expired"):
        complete(context, telegram)
    assert telegram.polls == []


def test_lock_prevents_competing_update_consumers(context):
    settings, _, _ = context
    telegram = FakeTelegram()
    with InstanceLock(settings.database.with_suffix(".lock")):
        with pytest.raises(RuntimeError, match="already running"):
            complete(context, telegram)
    assert telegram.polls == []


def test_old_processed_update_cannot_select_destination(context):
    settings, path, env_file = context
    old = env_file.read_bytes()
    store = Store(settings.database)
    store.set_meta("telegram_offset", "10")
    store.close()
    telegram = FakeTelegram([[message(1)]])
    assert complete(context, telegram) is None
    assert telegram.polls == [(10, 0)]
    assert env_file.read_bytes() == old and path.exists()


def test_failed_atomic_write_preserves_config_challenge_and_offset(context, monkeypatch):
    settings, path, env_file = context
    old = env_file.read_bytes()

    def fail_replace(*_):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(setup.os, "replace", fail_replace)
    with pytest.raises(OSError):
        complete(context, FakeTelegram([[message()]]))
    assert env_file.read_bytes() == old and path.exists()
    assert list(env_file.parent.glob(".env.telegram-*")) == []
    store = Store(settings.database)
    try:
        assert store.get_meta("telegram_offset") is None
    finally:
        store.close()


def test_waiting_is_bounded_and_zero_wait_performs_one_poll(context):
    telegram = FakeTelegram()
    assert complete(context, telegram, wait_seconds=0) is None
    assert telegram.polls == [(0, 0)]
    now = [0.0]
    telegram = FakeTelegram()
    assert (
        complete(
            context,
            telegram,
            wait_seconds=2,
            monotonic=lambda: now[0],
            sleep=lambda duration: now.__setitem__(0, now[0] + duration),
        )
        is None
    )
    assert now[0] == 2.0
    assert all(0 <= timeout <= 20 for _, timeout in telegram.polls)


@pytest.mark.parametrize("duration", [-1, 61, 1.5, True])
def test_invalid_wait_is_rejected(context, duration):
    with pytest.raises(setup.SetupError, match="between 0 and 60"):
        complete(context, FakeTelegram(), wait_seconds=duration)


def test_legacy_hex_challenge_and_bot_command_suffix_are_accepted(context):
    _, path, _ = context
    legacy = CHALLENGE.removeprefix("tower_setup_")
    path.write_text(json.dumps({"challenge": legacy, "created_at": 1000}))
    telegram = FakeTelegram([[message(text=f"/start@ExampleTowerBot {legacy}")]])
    assert complete(context, telegram)["connected"] is True


def test_missing_admin_setting_is_appended_preserving_other_config(context):
    _, _, env_file = context
    env_file.write_bytes(b"H1_API_TOKEN=fake-value")
    complete(context, FakeTelegram([[message()]]))
    assert env_file.read_bytes() == b"H1_API_TOKEN=fake-value\nTELEGRAM_ADMIN_CHAT_ID=123456789\n"


def test_cli_timeout_exit_three_and_success_output_hides_credentials(context, monkeypatch, capsys):
    settings, path, env_file = context
    path.write_text(json.dumps({"challenge": CHALLENGE, "created_at": time.time()}))
    monkeypatch.setattr(setup.Settings, "load", lambda *_: settings)
    telegram = FakeTelegram()
    monkeypatch.setattr(setup, "TelegramClient", lambda *_: telegram)
    args = ["--env-file", str(env_file), "complete", "--wait-seconds", "0"]
    assert setup.main(args) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "waiting_for_start"
    telegram.batches = [[message()]]
    assert setup.main(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["connected"] is True
    assert settings.telegram_token not in output and "123456789" not in output
