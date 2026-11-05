"""A successful program must not conceal another program's failed collection."""

import copy
from contextlib import closing
from types import SimpleNamespace

import pytest

from tower.config import Settings
from tower.health import health_status, operator_needs_attention, status_text
from tower.service import Monitor
from tower.source import SourceError
from tower.store import Store


def snapshot(number):
    return {
        "program": {
            "id": str(number),
            "handle": f"fixture_{number}",
            "name": "Fixture",
            "state": "public_mode",
        },
        "scopes": [],
        "exclusions": [],
    }


def heartbeats(store, now):
    for suffix in ("", "_catalog", "_source", "_source_2", "_delivery", "_bot"):
        store.set_meta("heartbeat" + suffix, now)


def test_other_program_success_cannot_hide_failure_and_retry_beats_old_backlog(tmp_path):
    values = [snapshot(number) for number in range(1, 4)]
    now = [1060]
    calls = []

    def fetch(program):
        calls.append(program["id"])
        if calls == ["1"]:
            raise SourceError("sensitive source response must never appear")
        return copy.deepcopy(values[int(program["id"]) - 1])

    with closing(Store(tmp_path / "monitor.db")) as store:
        store.sync_catalog([value["program"] for value in values], 1000)
        for value in values:
            store.apply_snapshot(value, 1000, 60)
        monitor = Monitor(
            store, SimpleNamespace(fetch_snapshot=fetch), Settings(), clock=lambda: now[0]
        )
        monitor.scope_tick()  # A fails and receives a scheduled retry.
        monitor.scope_tick()  # B succeeds, clearing only the legacy global signal.
        heartbeats(store, now[0])
        assert calls == ["1", "2"]
        assert store.get_meta("scope_error") == ""
        result = health_status(store, Settings(), now[0])
        assert result["freshness_percent"] == 100
        assert result["failed_programs"] == 1
        assert result["reasons"] == ["program_read_failures"]
        assert operator_needs_attention(store, Settings(), now[0])
        text = status_text(store, Settings(), now[0])
        assert "Programs waiting for a successful retry: 1" in text
        assert "sensitive" not in text and "fixture_1" not in text
        now[0] = 1120
        monitor.scope_tick()
        # C has been due since 1060, but the controlled retry receives priority.
        assert calls == ["1", "2", "1"]
        recovered = health_status(store, Settings(), now[0])
        assert recovered["failed_programs"] == 0
        assert recovered["healthy"] is True
        assert store.status(now[0])["event_count"] == 0


def test_99_percent_freshness_does_not_hide_small_group_of_failing_programs(tmp_path):
    now = 10000
    values = [snapshot(number) for number in range(1, 596)]
    with closing(Store(tmp_path / "coverage.db")) as store:
        store.sync_catalog([value["program"] for value in values], now - 2000)
        for number, value in enumerate(values):
            store.apply_snapshot(value, now - 1801 if number < 5 else now, 900)
        for value in values[:5]:
            store.record_program_failure(value["program"]["id"], now)
        store.sync_catalog([value["program"] for value in values], now)
        heartbeats(store, now)
        store.set_meta("scope_error", "")
        result = health_status(store, Settings(), now)
        assert result["freshness_percent"] == 99.16
        assert result["freshness_target_met"] is True
        assert result["failed_programs"] == 5
        assert result["healthy"] is result["ready"] is False
        assert result["reasons"] == ["program_read_failures"]


def test_confirmed_nonpublic_program_is_deactivated_without_retrying_private_data(tmp_path):
    value = snapshot(1)

    def fetch(program):
        raise SourceError("private response must not be stored", not_public=True)

    with closing(Store(tmp_path / "visibility.db")) as store:
        store.sync_catalog([value["program"]], 1000)
        store.apply_snapshot(value, 1000, 60)
        monitor = Monitor(
            store, SimpleNamespace(fetch_snapshot=fetch), Settings(), clock=lambda: 1060
        )
        monitor.scope_tick()
        result = store.status(1060)
        assert result["failed_programs"] == result["active_programs"] == 0
        assert store.get_meta("scope_error") == ""
        assert store.next_due(10**10) is None


def test_failed_read_and_retry_schedule_survive_monitor_restart(tmp_path):
    value = snapshot(1)
    other = snapshot(2)
    programs = [value["program"], other["program"]]
    path = tmp_path / "restart.db"

    def fail(program):
        raise SourceError("temporary response", retry_after=180, status_code=429)

    with closing(Store(path)) as store:
        store.sync_catalog(programs, 1000)
        store.apply_snapshot(value, 1000, 60)
        store.apply_snapshot(other, 1000, 60)
        monitor = Monitor(
            store, SimpleNamespace(fetch_snapshot=fail), Settings(), clock=lambda: 1060
        )
        monitor.scope_tick()
        assert monitor.cooldown == 1240
    calls = []
    catalog_calls = []

    def catalog():
        catalog_calls.append(True)
        return programs

    def succeed(program):
        calls.append(program["id"])
        return copy.deepcopy(value)

    with closing(Store(path)) as store:
        now = [1239]
        monitor = Monitor(
            store,
            SimpleNamespace(fetch_snapshot=succeed, list_programs=catalog),
            Settings(),
            clock=lambda: now[0],
        )
        monitor.tick()
        assert calls == catalog_calls == []
        assert store.status(now[0])["failed_programs"] == 1
        now[0] = 1240
        monitor.tick()
        assert calls == ["1"] and store.status(now[0])["failed_programs"] == 0
        assert catalog_calls == [True]


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "invalid-sensitive-value"])
def test_invalid_persisted_global_cooldown_fails_closed_without_exposing_value(tmp_path, value):
    with closing(Store(tmp_path / "invalid.db")) as store:
        store.set_meta("source_cooldown_until", value)
        with pytest.raises(ValueError, match="^Invalid persisted source cooldown$"):
            Monitor(store, SimpleNamespace(), Settings())


def test_program_backoff_does_not_extend_other_programs_global_cooldown(tmp_path):
    values = [snapshot(1), snapshot(2)]
    programs = [value["program"] for value in values]
    now = [1060]
    calls = []
    catalog_calls = []

    def fetch(program):
        calls.append(program["id"])
        if program["id"] == "1":
            raise SourceError("temporary response", retry_after=4, status_code=503)
        return copy.deepcopy(values[1])

    def catalog():
        catalog_calls.append(now[0])
        return programs

    with closing(Store(tmp_path / "isolated-backoff.db")) as store:
        store.sync_catalog(programs, 1000)
        for value in values:
            store.apply_snapshot(value, 1000, 60)
        monitor = Monitor(
            store,
            SimpleNamespace(fetch_snapshot=fetch, list_programs=catalog),
            Settings(),
            clock=lambda: now[0],
        )
        monitor.scope_tick()
        assert monitor.cooldown == 1120
        now[0] = 1120
        monitor.scope_tick()
        assert calls == ["1", "1"]
        assert monitor.cooldown == 1180
        now[0] = 1180
        monitor.tick()
        assert catalog_calls == [1180]
        assert calls == ["1", "1", "2"]
        assert store.status(now[0])["failed_programs"] == 1
        assert store.next_due(1239) is None
        assert store.next_due(1240)["id"] == "1"
