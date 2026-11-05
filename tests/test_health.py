"""Readable bot status uses the same operational signals as the CLI."""

import pytest

from tower.config import Settings
from tower.health import health_status, status_text
from tower.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "health.db")
    yield database
    database.close()


def running(store, now=10000):
    program = {"id": "1", "handle": "fixture", "name": "Fixture", "state": "public_mode"}
    store.sync_catalog([program], now)
    for name in (
        "heartbeat",
        "heartbeat_catalog",
        "heartbeat_source",
        "heartbeat_source_2",
        "heartbeat_delivery",
        "heartbeat_bot",
    ):
        store.set_meta(name, now)
    return {"program": program, "scopes": [], "exclusions": []}


def test_status_distinguishes_partial_collection_and_ready_without_private_details(store):
    snapshot = running(store)
    store.set_subscription("912345678")
    partial = status_text(store, now=10000)
    assert "Building initial coverage" in partial
    assert "Public programs collected: 0 / 1" in partial
    assert "Still collecting" in partial
    assert "Monitoring active" not in partial
    store.apply_snapshot(snapshot, 10000, 900)
    complete = status_text(store, now=10020)
    assert "Monitoring active" in complete
    assert "Public programs collected: 1 / 1" in complete
    assert "Program catalog checked: 20s ago" in complete
    assert "Initial collection: Complete" in complete
    assert "912345678" not in complete
    assert "subscriptions" not in complete
    assert "heartbeat_" not in complete


def test_status_reports_stalled_worker_even_with_complete_data_and_main_heartbeat(store):
    snapshot = running(store)
    store.apply_snapshot(snapshot, 10000, 900)
    store.set_meta("heartbeat_delivery", 9800)
    text = status_text(store, now=10000)
    assert "Monitoring needs attention" in text
    assert "background worker has not checked in recently" in text
    assert "Initial collection: Complete" in text
    assert "Monitoring active" not in text
    assert health_status(store, Settings(), 10000)["ready"] is False


def test_status_reports_delay_and_safe_error_explanations(store, monkeypatch):
    snapshot = running(store)
    store.apply_snapshot(snapshot, 10000, 900)
    monkeypatch.setattr("tower.store.time.time", lambda: 9000)
    store.queue_text("123", "private message content", "fixture")
    store.set_meta("scope_error", "never expose stored diagnostic values")
    text = status_text(store, now=10000)
    assert "Messages waiting: 1" in text
    assert "16m 40s ago" in text
    assert "scope read failed" in text
    assert "waiting longer than expected" in text
    assert "private message content" not in text
    assert "never expose" not in text


def test_bot_status_uses_configured_thresholds_and_wall_clock(store, monkeypatch):
    snapshot = running(store)
    store.apply_snapshot(snapshot, 10000, 900)
    store.set_meta("heartbeat", 11000)
    for name in ("source", "delivery", "bot", "catalog"):
        store.set_meta(f"heartbeat_{name}", 11000)
    monkeypatch.setattr("tower.health.time.time", lambda: 11000)
    assert "Monitoring needs attention" in status_text(store)
    assert "Monitoring active" in status_text(store, Settings(catalog_interval=600))


def test_unstarted_status_is_explicit_about_missing_observations(store):
    text = status_text(store, now=10000)
    assert "Monitoring needs attention" in text
    assert "Not available yet" in text
    assert "Initial collection: Still collecting" in text
    assert "Coverage includes public programs returned by the HackerOne API" in text
