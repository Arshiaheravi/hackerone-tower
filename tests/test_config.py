"""Configuration parsing, validation, and credential disclosure boundaries."""

from pathlib import Path

import pytest

from tower.config import ConfigError, Settings, read_env


def test_env_bom_quotes_equals_hashes_and_literal_variables(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nH1_USERNAME = 'example_user'\n"
        'H1_API_TOKEN="opaque+base64==#literal"\n'
        "TOWER_DATABASE='folder with spaces/tower.sqlite3'\n"
        "LITERAL=${DO_NOT_EXPAND}\n",
        encoding="utf-8-sig",
    )
    values = read_env(path)
    assert values["H1_USERNAME"] == "example_user"
    assert values["H1_API_TOKEN"] == "opaque+base64==#literal"
    assert values["TOWER_DATABASE"] == "folder with spaces/tower.sqlite3"
    assert values["LITERAL"] == "${DO_NOT_EXPAND}"


def test_process_environment_overrides_file_including_empty_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("H1_USERNAME=file_user\nH1_API_TOKEN=file-token\n")
    settings = Settings.load(path, environ={"H1_USERNAME": "process_user", "H1_API_TOKEN": ""})
    assert settings.username == "process_user"
    assert settings.api_token == ""
    with pytest.raises(ConfigError, match="H1_API_TOKEN"):
        settings.require_live()


def test_actual_environment_is_used_when_mapping_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("H1_USERNAME", "environment_user")
    settings = Settings.load(tmp_path / "absent.env")
    assert settings.username == "environment_user"


def test_missing_env_file_supports_offline_defaults(tmp_path):
    path = tmp_path / "absent.env"
    assert read_env(path) == {}
    settings = Settings.load(path, environ={})
    assert settings.database == Path("data/tower.sqlite3")
    assert settings.scope_requests_per_minute == 40
    assert settings.scope_workers == 2
    assert settings.personal_bot is True


@pytest.mark.parametrize("workers", [1, 2])
def test_configured_scope_worker_count(tmp_path, workers):
    settings = Settings.load(tmp_path / "absent.env", environ={"SCOPE_WORKERS": str(workers)})
    assert settings.scope_workers == workers


@pytest.mark.parametrize("line", ["not-an-assignment-secret", "lowercase=secret", "BAD KEY=secret"])
def test_invalid_env_lines_report_location_without_echoing_content(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text("# header\n" + line)
    with pytest.raises(ConfigError) as error:
        read_env(path)
    assert "line 2" in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CATALOG_INTERVAL_SECONDS", "59"),
        ("CATALOG_INTERVAL_SECONDS", "86401"),
        ("SCOPE_REFRESH_SECONDS", "0"),
        ("SCOPE_REFRESH_SECONDS", "86401"),
        ("SCOPE_WORKERS", "0"),
        ("SCOPE_WORKERS", "3"),
        ("SCOPE_WORKERS", "1.5"),
        ("SCOPE_WORKERS", "true"),
        ("H1_REQUESTS_PER_MINUTE", "0"),
        ("H1_REQUESTS_PER_MINUTE", "501"),
        ("H1_SCOPE_REQUESTS_PER_MINUTE", "0"),
        ("H1_SCOPE_REQUESTS_PER_MINUTE", "41"),
        ("RETENTION_DAYS", "6"),
        ("RETENTION_DAYS", "3651"),
        ("CATALOG_INTERVAL_SECONDS", "not-a-number"),
        ("ENABLE_PERSONAL_BOT", "yes"),
        ("TELEGRAM_CHANNEL_ID", "https://t.me/example"),
        ("TELEGRAM_ADMIN_CHAT_ID", "-100123"),
        ("TELEGRAM_ADMIN_CHAT_ID", "0"),
    ],
)
def test_invalid_settings_rejected_with_named_field(tmp_path, key, value):
    with pytest.raises(ConfigError, match=key):
        Settings.load(tmp_path / "absent.env", environ={key: value})


@pytest.mark.parametrize("channel", ["-100123456789", "@example_channel"])
def test_channel_only_live_configuration(tmp_path, channel):
    settings = Settings.load(
        tmp_path / "absent.env",
        environ={
            "H1_USERNAME": "example",
            "H1_API_TOKEN": "fake-h1-test-value",
            "TELEGRAM_BOT_TOKEN": "123:fake-telegram-test-value",
            "TELEGRAM_CHANNEL_ID": channel,
            "TELEGRAM_ADMIN_CHAT_ID": "1234",
            "ENABLE_PERSONAL_BOT": "FALSE",
        },
    )
    settings.require_live()
    assert settings.channel_id == channel
    assert settings.personal_bot is False


def test_tokens_never_appear_in_settings_repr_or_validation_errors(tmp_path):
    settings = Settings.load(
        tmp_path / "absent.env",
        environ={
            "H1_API_TOKEN": "fake-h1-sensitive-value",
            "TELEGRAM_BOT_TOKEN": "123:fake-telegram-sensitive-value",
        },
    )
    assert "fake-h1-sensitive-value" not in repr(settings)
    assert "fake-telegram-sensitive-value" not in repr(settings)
    with pytest.raises(ConfigError) as error:
        settings.require_live()
    assert "H1_USERNAME" in str(error.value)
    assert "sensitive-value" not in str(error.value)


@pytest.mark.parametrize("missing", ["H1_USERNAME", "H1_API_TOKEN", "TELEGRAM_BOT_TOKEN"])
def test_live_mode_identifies_each_missing_required_setting(tmp_path, missing):
    values = {
        "H1_USERNAME": "example",
        "H1_API_TOKEN": "fake-h1",
        "TELEGRAM_BOT_TOKEN": "123:fake-telegram",
    }
    del values[missing]
    settings = Settings.load(tmp_path / "absent.env", environ=values)
    with pytest.raises(ConfigError, match=missing):
        settings.require_live()


def test_live_mode_requires_channel_or_personal_subscriptions():
    settings = Settings(
        username="example",
        api_token="fake-h1",
        telegram_token="123:fake-telegram",
        personal_bot=False,
    )
    with pytest.raises(ConfigError, match="channel"):
        settings.require_live()
