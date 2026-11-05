"""Freshness is measured across the catalog, not inferred from one healthy worker."""

from contextlib import closing
from types import SimpleNamespace

import pytest

from tower.config import Settings
from tower.health import health_status, operator_needs_attention, status_text
from tower.service import Service
from tower.store import Store


def program(number):
    return {
        "id": str(number),
        "handle": f"fixture_{number}",
        "name": "Fixture",
        "state": "public_mode",
    }


def snapshot(value):
    return {"program": value, "scopes": [], "exclusions": []}


def heartbeat(store, now):
    for name in ("", "_catalog", "_source", "_source_2", "_delivery", "_bot"):
        store.set_meta("heartbeat" + name, now)


def test_stale_program_is_detected_despite_fresh_progress_and_recovers_after_refresh(tmp_path):
    now = 10000
    programs = [program(1), program(2)]
    with closing(Store(tmp_path / "freshness.db")) as store:
        store.sync_catalog(programs, now - 2000)
        store.apply_snapshot(snapshot(programs[0]), now - 1801, 900)
        store.apply_snapshot(snapshot(programs[1]), now, 900)
        store.sync_catalog(programs, now)
        heartbeat(store, now)
        store.set_meta("scope_error", "")
        result = health_status(store, Settings(), now)
        assert result["scope_progress_age_seconds"] == 0
        assert result["baseline_complete"] is True
        assert result["fresh_programs"] == result["stale_programs"] == 1
        assert result["freshness_percent"] == 50
        assert result["freshness_target_met"] is False
        assert result["reasons"] == ["scope_freshness_target_missed"]
        assert result["healthy"] is result["ready"] is False
        assert operator_needs_attention(store, Settings(), now) is True
        text = status_text(store, Settings(), now)
        assert "50.00% (1 / 2)" in text
        assert "99% checked within 30 minutes" in text
        assert "not the percentage of all changes detected" in text
        store.apply_snapshot(snapshot(programs[0]), now, 900)
        recovered = health_status(store, Settings(), now)
        assert recovered["freshness_percent"] == 100
        assert recovered["stale_programs"] == 0
        assert recovered["healthy"] is recovered["ready"] is True
        assert operator_needs_attention(store, Settings(), now) is False


def test_uncollected_programs_are_in_denominator_without_false_startup_outage(tmp_path):
    now = 10000
    with closing(Store(tmp_path / "initial.db")) as store:
        programs = [program(1), program(2)]
        store.sync_catalog(programs, now)
        store.apply_snapshot(snapshot(programs[0]), now, 900)
        heartbeat(store, now)
        result = health_status(store, Settings(), now)
        assert result["freshness_percent"] == 50
        assert result["stale_programs"] == 0
        assert result["uncollected_programs"] == 1
        assert result["healthy"] is True
        assert result["ready"] is False


def test_uncollected_program_eventually_warns_even_when_other_programs_advance(tmp_path):
    now = 10000
    with closing(Store(tmp_path / "uncollected.db")) as store:
        programs = [program(1), program(2)]
        store.sync_catalog(programs, now - 1801)
        store.apply_snapshot(snapshot(programs[0]), now, 900)
        store.sync_catalog(programs, now)
        heartbeat(store, now)
        result = health_status(store, Settings(), now)
        assert result["overdue_uncollected_programs"] == 1
        assert result["baseline_complete"] is False
        assert result["reasons"] == ["scope_freshness_target_missed"]
        assert operator_needs_attention(store, Settings(), now) is True


def test_first_catalog_has_no_operator_outage_until_it_fails(tmp_path):
    with closing(Store(tmp_path / "starting.db")) as store:
        heartbeat(store, 10000)
        assert operator_needs_attention(store, Settings(), 10000) is False
        store.set_meta("catalog_error", "source_unavailable")
        assert operator_needs_attention(store, Settings(), 10000) is True


def test_empty_catalog_does_not_claim_100_percent_freshness(tmp_path):
    with closing(Store(tmp_path / "empty.db")) as store:
        store.sync_catalog([], 10000)
        heartbeat(store, 10000)
        result = health_status(store, Settings(), 10000)
        assert result["freshness_percent"] is None
        assert result["freshness_target_met"] is None
        assert "No public programs available" in status_text(store, now=10000)


def test_stale_program_operator_warning_is_short_and_deduplicated_across_restart(tmp_path):
    now = 7200
    settings = Settings(admin_chat_id="123456")
    path = tmp_path / "operator.db"
    with closing(Store(path)) as store:
        programs = [program(1), program(2)]
        store.sync_catalog(programs, now - 2000)
        store.apply_snapshot(snapshot(programs[0]), now - 1801, 900)
        store.apply_snapshot(snapshot(programs[1]), now, 900)
        store.sync_catalog(programs, now)
        heartbeat(store, now)
        service = Service(settings, store, SimpleNamespace(), SimpleNamespace())
        service.health_tick(now)
        service.health_tick(now + 30)
        service.health_tick(now + 60)
        assert store.status(now)["pending_deliveries"] == 1
        rows = store.pending_deliveries(10**12)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == settings.admin_chat_id
        assert "/status" in rows[0]["text"] and len(rows[0]["text"]) < 120
        assert "fixture_" not in rows[0]["text"]
    with closing(Store(path)) as reopened:
        service = Service(settings, reopened, SimpleNamespace(), SimpleNamespace())
        service.health_tick(now + 120)
        assert reopened.status(now)["pending_deliveries"] == 1
        service.health_tick(now + 3600)
        assert reopened.status(now)["pending_deliveries"] == 2


def test_health_warning_does_not_send_without_configured_operator(tmp_path):
    with closing(Store(tmp_path / "no_operator.db")) as store:
        service = Service(Settings(), store, SimpleNamespace(), SimpleNamespace())
        service.health_tick(10000)
        assert store.status(10000)["pending_deliveries"] == 0


def test_single_worker_mode_does_not_require_a_second_source_heartbeat(tmp_path):
    with closing(Store(tmp_path / "single.db")) as store:
        store.sync_catalog([], 10000)
        heartbeat(store, 10000)
        store.set_meta("heartbeat_source_2", "")
        assert health_status(store, Settings(scope_workers=1), 10000)["healthy"] is True
        assert health_status(store, Settings(scope_workers=2), 10000)["healthy"] is False


def test_freshness_boundary_and_restart_preserve_journal(tmp_path):
    path = tmp_path / "restart.db"
    now = 10000
    programs = [program(number) for number in range(1, 101)]
    with closing(Store(path)) as store:
        store.sync_catalog(programs, now - 2000)
        for value in programs:
            store.apply_snapshot(
                snapshot(value), now - (1801 if value["id"] == "100" else 1800), 900
            )
        before_events = store.status(now)["event_count"]
        result = store.status(now)
        assert result["freshness_percent"] == 99
        assert result["freshness_target_met"] is True
        assert result["stale_programs"] == 1
        assert store.status(now + 1)["freshness_target_met"] is False
    with closing(Store(path)) as reopened:
        assert reopened.status(now) == result
        assert reopened.status(now)["event_count"] == before_events == 0
        assert reopened.apply_snapshot(snapshot(programs[0]), now, 900) == []


def test_inactive_programs_do_not_distort_freshness_and_future_dates_are_not_fresh(tmp_path):
    now = 10000
    programs = [program(1), program(2)]
    with closing(Store(tmp_path / "inactive.db")) as store:
        store.sync_catalog(programs, now - 2000)
        store.apply_snapshot(snapshot(programs[0]), now - 1900, 900)
        store.apply_snapshot(snapshot(programs[1]), now + 121, 900)
        store.deactivate_program("1", now)
        result = store.status(now)
        assert result["active_programs"] == result["stale_programs"] == 1
        assert result["freshness_percent"] == 0


def test_configured_refresh_interval_sets_honest_freshness_window(tmp_path):
    now = 10000
    with closing(Store(tmp_path / "configured.db")) as store:
        store.sync_catalog([program(1)], now - 3000)
        store.apply_snapshot(snapshot(program(1)), now - 2000, 3600)
        store.sync_catalog([program(1)], now)
        heartbeat(store, now)
        result = health_status(store, Settings(scope_refresh=3600), now)
        assert result["freshness_window_seconds"] == 7200
        assert result["freshness_percent"] == 100
        assert result["healthy"] is True


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf")])
def test_freshness_rejects_invalid_window(tmp_path, window):
    with closing(Store(tmp_path / "invalid.db")) as store:
        with pytest.raises(ValueError, match="Invalid freshness window"):
            store.status(10000, window)
