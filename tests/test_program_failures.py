"""Program failures remain visible and retryable until that program recovers."""

from contextlib import closing

import pytest

from tower.store import Store


def snapshot(identifier):
    return {
        "program": {
            "id": str(identifier),
            "handle": f"fixture_{identifier}",
            "name": "Fixture",
            "state": "public_mode",
        },
        "scopes": [
            {
                "id": "10",
                "asset_identifier": "asset.example.test",
                "asset_type": "URL",
                "eligible_for_submission": True,
                "eligible_for_bounty": True,
            }
        ],
        "exclusions": [],
    }


def baseline(store, count=2):
    values = [snapshot(number) for number in range(1, count + 1)]
    store.sync_catalog([value["program"] for value in values], 100)
    for value in values:
        store.apply_snapshot(value, 100, 10)
    return values


def failure(store, identifier="1"):
    row = store._db.execute(
        "SELECT * FROM program_failures WHERE program_id=?", (identifier,)
    ).fetchone()
    return dict(row) if row else None


def schedule(store, identifier="1"):
    return tuple(
        store._db.execute(
            "SELECT due_at,priority FROM programs WHERE id=?", (identifier,)
        ).fetchone()
    )


def test_other_success_cannot_clear_failure_and_restart_preserves_retry_and_deliveries(tmp_path):
    path = tmp_path / "failures.db"
    with closing(Store(path)) as store:
        values = baseline(store)
        store.queue_text("123", "Pending reply", "pending")
        assert store.record_program_failure("1", 200) == 260
        expected = failure(store)
        assert expected == {
            "program_id": "1",
            "failure_count": 1,
            "first_failed_at": 200,
            "last_failed_at": 200,
            "reason": "source_unavailable",
            "retry_at": 260,
        }
        store.apply_snapshot(values[1], 201, 900)
        assert failure(store) == expected
        status = store.status(201)
        assert status["freshness_percent"] == 100
        assert status["failed_programs"] == 1
        assert status["oldest_program_failure_age_seconds"] == 1
        assert store.next_due(259) is None
    with closing(Store(path)) as store:
        assert failure(store) == expected
        assert schedule(store) == (260, 1)
        assert store.status(220)["pending_deliveries"] == 1
        assert store.next_due(260)["id"] == "1"
        assert store.record_program_failure("1", 260, reason="snapshot_rejected") == 380
        assert failure(store)["first_failed_at"] == 200
        assert failure(store)["failure_count"] == 2
        assert failure(store)["reason"] == "snapshot_rejected"
        store.apply_snapshot(values[0], 380, 900)
        assert failure(store) is None
        assert store.status(380)["failed_programs"] == 0
        assert store.status(380)["oldest_program_failure_age_seconds"] is None
        assert store.record_program_failure("1", 400) == 460
        assert failure(store)["failure_count"] == 1


def test_retry_backoff_is_bounded_and_keeps_previous_complete_data(tmp_path):
    with closing(Store(tmp_path / "backoff.db")) as store:
        baseline(store, count=1)
        original = store.get_program("fixture_1")
        now = 200
        for count, delay in enumerate([60, 120, 240, 480, 960, 1920, 3600, 3600], 1):
            retry_at = store.record_program_failure("1", now)
            assert retry_at == now + delay
            assert failure(store)["failure_count"] == count
            assert failure(store)["first_failed_at"] == 200
            assert store.next_due(retry_at - 1) is None
            assert store.next_due(retry_at)["id"] == "1"
            assert store.get_program("fixture_1") == original
            assert store.status(now)["event_count"] == 0
            now = retry_at


def test_server_retry_floor_survives_catalog_edits_defer_and_restart(tmp_path):
    path = tmp_path / "retry_floor.db"
    with closing(Store(path)) as store:
        values = baseline(store, count=1)
        assert store.record_program_failure("1", 200, retry_after=7200) == 7400
        values[0]["program"]["name"] = "Changed public name"
        store.sync_catalog([values[0]["program"]], 250)
        assert schedule(store) == (7400, 1)
        assert store.record_program_failure("1", 260) == 7400
        store.defer_program("1", 300)
        assert schedule(store)[0] == 7400
    with closing(Store(path)) as store:
        store.sync_catalog([values[0]["program"]], 300)
        assert store.next_due(7399) is None
        assert store.next_due(7400)["id"] == "1"
        assert failure(store)["retry_at"] == 7400


def test_failed_retries_follow_discoveries_without_starving_normal_refreshes(tmp_path):
    with closing(Store(tmp_path / "fairness.db")) as store:
        values = baseline(store, count=7)
        for number in range(2, 8):
            store.record_program_failure(str(number), 200)
        new = snapshot(8)
        store.sync_catalog([value["program"] for value in [*values, new]], 300)
        assert store.next_due(300)["id"] == "8"
        store.apply_snapshot(new, 300, 900)
        for number in range(2, 6):
            assert store.next_due(300)["id"] == str(number)
            store.apply_snapshot(values[number - 1], 300, 900)
        assert store.next_due(300)["id"] == "1"
        store.apply_snapshot(values[0], 300, 900)
        assert store.next_due(300)["id"] == "6"


def test_failed_discovery_retries_at_bounded_priority(tmp_path):
    with closing(Store(tmp_path / "discovery.db")) as store:
        values = baseline(store, count=1)
        new = snapshot(2)
        store.sync_catalog([values[0]["program"], new["program"]], 200)
        assert schedule(store, "2") == (200, 2)
        store.record_program_failure("2", 200)
        assert schedule(store, "2") == (260, 1)
        assert store.get_meta("priority_streak") == "1"


@pytest.mark.parametrize("method", ["detail", "catalog"])
def test_nonpublic_program_clears_failures_cancels_alerts_and_reappears_cleanly(tmp_path, method):
    with closing(Store(tmp_path / "privacy.db")) as store:
        values = baseline(store, count=1)
        store.set_subscription("123")
        values[0]["scopes"][0]["asset_identifier"] = "new.example.test"
        store.apply_snapshot(values[0], 150, 900)
        store.record_program_failure("1", 200)
        assert store.status(200)["pending_deliveries"] == 1
        if method == "detail":
            store.deactivate_program("1", 210)
        else:
            store.sync_catalog([], 210)
        assert failure(store) is None
        assert store.status(210)["failed_programs"] == 0
        assert store.status(210)["pending_deliveries"] == 0
        assert store.record_program_failure("1", 211) is None
        assert store.next_due(10000) is None
        store.sync_catalog([values[0]["program"]], 220)
        assert store.next_due(220)["id"] == "1"
        store.record_program_failure("1", 220)
        assert failure(store)["failure_count"] == 1
        assert failure(store)["first_failed_at"] == 220


def test_unknown_program_does_not_create_failure(tmp_path):
    with closing(Store(tmp_path / "unknown.db")) as store:
        assert store.record_program_failure("missing", 200) is None
        assert store.status(200)["failed_programs"] == 0


def test_failure_record_and_retry_schedule_roll_back_together(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "failure_rollback.db")) as store:
        baseline(store, count=1)

        def interrupted(_priority):
            raise RuntimeError("Transaction interrupted")

        monkeypatch.setattr(store, "_record_priority_turn", interrupted)
        with pytest.raises(RuntimeError, match="Transaction interrupted"):
            store.record_program_failure("1", 200)
        assert failure(store) is None
        assert schedule(store) == (110, 0)


@pytest.mark.parametrize("failure_stage", ["render", "commit_state"])
def test_failed_snapshot_commit_preserves_failure_snapshot_and_outbox(
    tmp_path, monkeypatch, failure_stage
):
    with closing(Store(tmp_path / "snapshot_rollback.db")) as store:
        values = baseline(store, count=1)
        store.set_subscription("123")
        store.record_program_failure("1", 200)
        before = failure(store)
        original = store.get_program("fixture_1")
        values[0]["scopes"][0]["asset_identifier"] = "changed.example.test"

        def interrupted(_value):
            raise RuntimeError("Snapshot transaction interrupted")

        if failure_stage == "render":
            monkeypatch.setattr("tower.store.render_event", interrupted)
        else:
            monkeypatch.setattr(store, "_record_priority_turn", interrupted)
        with pytest.raises(RuntimeError, match="Snapshot transaction interrupted"):
            store.apply_snapshot(values[0], 260, 900)
        assert failure(store) == before
        assert schedule(store) == (260, 1)
        assert store.get_program("fixture_1") == original
        assert store.status(260)["event_count"] == 0
        assert store.status(260)["pending_deliveries"] == 0


def test_recovery_clears_only_the_matching_program_failure(tmp_path):
    with closing(Store(tmp_path / "recovery.db")) as store:
        values = baseline(store)
        store.record_program_failure("1", 200)
        store.record_program_failure("2", 201)
        store.apply_snapshot(values[1], 270, 900)
        assert failure(store, "2") is None
        assert failure(store, "1")["first_failed_at"] == 200
        assert store.status(270)["failed_programs"] == 1
        assert store.status(270)["oldest_program_failure_age_seconds"] == 70


@pytest.mark.parametrize(
    "options",
    [
        {"now": float("nan")},
        {"now": float("inf")},
        {"retry_after": float("nan")},
        {"retry_after": float("inf")},
        {"retry_after": -1},
        {"now": 1e308, "retry_after": 1e308},
        {"reason": "Raw exception containing private data"},
    ],
)
def test_failure_validation_never_persists_invalid_or_private_diagnostics(tmp_path, options):
    with closing(Store(tmp_path / "validation.db")) as store:
        baseline(store, count=1)
        with pytest.raises(ValueError):
            store.record_program_failure("1", **({"now": 200} | options))
        assert failure(store) is None
        assert schedule(store) == (110, 0)
