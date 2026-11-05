import json
import sqlite3
from copy import deepcopy

import pytest

from tower.store import Store


def snapshot(program_id="1", handle="example"):
    return {
        "program": {
            "id": program_id,
            "handle": handle,
            "state": "public_mode",
            "name": "Example",
            "offers_bounties": True,
        },
        "scopes": [
            {
                "id": "10",
                "asset_identifier": "example.com",
                "asset_type": "URL",
                "eligible_for_bounty": True,
            }
        ],
        "exclusions": [],
    }


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "tower.db")
    yield value
    value.close()


@pytest.fixture
def legacy_multipart_renderer(monkeypatch):
    """Keep persisted legacy delivery behavior independent of today's alert format."""
    monkeypatch.setattr(
        "tower.store.render_event",
        lambda event: [f"Legacy event {event['id']} part 1", f"Legacy event {event['id']} part 2"],
    )


def baseline(store, now=100):
    value = snapshot()
    store.sync_catalog([value["program"]], now)
    assert store.apply_snapshot(value, now + 1, 60) == []
    return value


def change(store, old=None, now=200):
    value = deepcopy(old or snapshot())
    value["scopes"][0]["instruction"] = "New scope instructions"
    return value, store.apply_snapshot(value, now, 60)


def test_initial_baseline_silent_and_restart_idempotent(tmp_path):
    path = tmp_path / "tower.db"
    store = Store(path)
    store.set_subscription("1")
    value = baseline(store)
    value, events = change(store, value)
    assert len(events) == 1
    assert store.status(200)["pending_deliveries"] > 0
    store.close()
    restarted = Store(path)
    assert restarted.apply_snapshot(value, 250, 60) == []
    assert restarted.status(250)["event_count"] == 1
    assert restarted.get_subscription("1")["active"]
    restarted.close()


@pytest.mark.parametrize("changed_field", ["policy", "instruction"])
def test_compact_alert_retains_full_snapshot_and_before_after_audit_across_restart(
    tmp_path, changed_field
):
    path = tmp_path / "compact.db"
    old = snapshot()
    old["program"]["policy"] = "Original public policy. " * 1000 + "OLD_POLICY_END"
    old["scopes"][0]["instruction"] = "Original scope instructions. " * 1000 + "OLD_SCOPE_END"
    current = deepcopy(old)
    if changed_field == "policy":
        current["program"]["policy"] = "Revised public policy. " * 1000 + "NEW_POLICY_END"
        before, after = old["program"], current["program"]
    else:
        current["scopes"][0]["instruction"] = (
            "Revised scope instructions. " * 1000 + "NEW_SCOPE_END"
        )
        before, after = old["scopes"][0], current["scopes"][0]

    store = Store(path)
    try:
        store.sync_catalog([old["program"]], 100)
        assert store.apply_snapshot(old, 100, 60) == []
        store.set_subscription("1")
        events = store.apply_snapshot(current, 200, 60)
        assert len(events) == 1
        event = events[0]
        assert event["before"] == before
        assert event["after"] == after
        assert event["changes"][changed_field] == {
            "before": before[changed_field],
            "after": after[changed_field],
        }
        assert store.get_program("example") == current
        assert store.recent_events() == [event]
        queued = store.pending_deliveries(200)
        assert store.status(200)["pending_deliveries"] == len(queued) == 1
        assert queued[0]["event_id"] == event["id"] and queued[0]["part"] == 1
        text = queued[0]["text"]
        assert "https://hackerone.com/example" in text
        assert len(text.encode("utf-16-le")) // 2 <= 800
        assert len(text.splitlines()) <= 6
        assert all(
            marker not in text
            for marker in (
                '"policy":',
                '"instruction":',
                '"asset_identifier":',
                "CURRENT PUBLIC PROGRAM DETAILS",
                "PREVIOUS RECORD",
                "OBSERVED RECORD",
                "OLD_POLICY_END",
                "OLD_SCOPE_END",
                "NEW_POLICY_END",
                "NEW_SCOPE_END",
            )
        )
    finally:
        store.close()

    restarted = Store(path)
    try:
        assert restarted.get_program("example") == current
        assert restarted.recent_events() == [event]
        assert restarted.pending_deliveries(250) == queued
        assert restarted.apply_snapshot(current, 250, 60) == []
        assert restarted.status(250)["event_count"] == 1
        assert restarted.status(250)["pending_deliveries"] == 1
    finally:
        restarted.close()


def test_new_program_discovery_after_first_catalog(store):
    baseline(store)
    new = snapshot("2", "new_program")
    store.set_subscription("1")
    store.sync_catalog([snapshot()["program"], new["program"]], 200)
    events = store.apply_snapshot(new, 201, 60)
    assert [event["kind"] for event in events] == ["program_discovered"]
    assert events[0]["after"]["scopes"] == new["scopes"]
    assert store.pending_deliveries(201)


def test_existing_multipart_alert_finishes_unchanged_before_new_compact_alert(
    tmp_path, monkeypatch
):
    path = tmp_path / "mixed-alert-formats.db"
    store = Store(path)
    try:
        baseline(store)
        store.set_subscription("1")
        legacy_parts = ["Persisted legacy part one", "Persisted legacy part two"]
        with monkeypatch.context() as legacy:
            legacy.setattr("tower.store.render_event", lambda event: legacy_parts)
            current, legacy_events = change(store)
        first = store.pending_deliveries(200)[0]
        store.delivered(first["id"], 1, 200)
        remaining = store.pending_deliveries(200)[0]
        store.delivery_failed(remaining["id"], 200, retry_after=30)
    finally:
        store.close()

    restarted = Store(path)
    try:
        current["scopes"][0]["instruction"] = "A new compact change"
        compact_events = restarted.apply_snapshot(current, 201, 60)
        assert len(compact_events) == 1
        assert restarted.status(201)["pending_deliveries"] == 2
        assert restarted.pending_deliveries(229) == []
        queued = restarted.pending_deliveries(230)
        assert len(queued) == 1
        assert queued[0]["event_id"] == legacy_events[0]["id"]
        assert queued[0]["text"] == legacy_parts[1]
        assert queued[0]["part"] == 2 and queued[0]["attempts"] == 1
        restarted.delivered(queued[0]["id"], 2, 230)
        queued = restarted.pending_deliveries(230)
        assert len(queued) == 1
        assert queued[0]["event_id"] == compact_events[0]["id"]
        assert queued[0]["part"] == 1
        assert "Instructions updated" in queued[0]["text"]
        assert "example.com" in queued[0]["text"]
        assert len(queued[0]["text"].encode("utf-16-le")) // 2 <= 800
        assert len(queued[0]["text"].splitlines()) == 6
    finally:
        restarted.close()


def test_catalog_absence_cancels_pending_without_removal_event(store):
    baseline(store)
    store.set_subscription("1")
    change(store)
    store.sync_catalog([], 300)
    assert store.pending_deliveries(300) == []
    assert store.get_program("example") is None
    assert store.recent_events() == []
    assert store.status(300)["event_count"] == 1
    with pytest.raises(ValueError):
        store.apply_snapshot(snapshot(), 301, 60)


def test_invalid_catalog_does_not_mutate_good_catalog(store):
    value = baseline(store)
    invalid = deepcopy(value["program"])
    invalid["state"] = "private_mode"
    with pytest.raises(ValueError):
        store.sync_catalog([invalid], 200)
    assert store.get_program("example") == value
    with pytest.raises(ValueError):
        store.sync_catalog([value["program"], value["program"]], 200)
    assert store.status(200)["last_catalog_at"] == 100


def test_render_failure_rolls_back_entire_snapshot_event_and_outbox(store, monkeypatch):
    value = baseline(store)
    store.set_subscription("1")

    def fail(event):
        raise RuntimeError("render error")

    monkeypatch.setattr("tower.store.render_event", fail)
    with pytest.raises(RuntimeError):
        change(store)
    assert store.get_program("example") == value
    assert store.status(200)["event_count"] == 0
    assert store.status(200)["pending_deliveries"] == 0


def test_incomplete_and_duplicate_snapshot_cannot_generate_removals(store):
    old = baseline(store)
    value = deepcopy(old)
    del value["exclusions"]
    with pytest.raises(ValueError):
        store.apply_snapshot(value, 200, 60)
    value = deepcopy(old)
    value["scopes"].append(deepcopy(value["scopes"][0]))
    with pytest.raises(ValueError):
        store.apply_snapshot(value, 200, 60)
    assert store.get_program("example") == old
    assert store.recent_events() == []


def test_delivery_retries_block_later_parts_but_not_other_chats(store):
    store.queue_text("1", "first", "one")
    store.queue_text("1", "second", "two")
    store.queue_text("2", "independent", "three")
    pending = store.pending_deliveries(100)
    assert [item["text"] for item in pending] == ["first", "independent"]
    first = pending[0]
    store.delivery_failed(first["id"], 100, retry_after=30)
    assert [item["text"] for item in store.pending_deliveries(129)] == ["independent"]
    assert store.pending_deliveries(130)[0]["attempts"] == 1
    store.delivered(first["id"], 500, 131)
    assert [item["text"] for item in store.pending_deliveries(131)] == ["second", "independent"]


def test_dead_letter_recovery_and_idempotent_command_reply(store):
    store.queue_text("1", "hello", "update:50")
    store.queue_text("1", "hello", "update:50")
    assert store.status(0)["pending_deliveries"] == 1
    delivery = store.pending_deliveries(0)[0]
    store.delivery_failed(delivery["id"], 0, permanent=True)
    assert store.pending_deliveries(100) == []
    assert store.status(100)["dead_deliveries"] == 1
    assert store.retry_dead() == 1
    assert store.pending_deliveries(100)[0]["attempts"] == 1


def test_subscription_filters_and_pause_cancel_pending(store):
    baseline(store)
    store.set_subscription("all")
    store.set_subscription("watch", filters={"handles": ["unrelated"]})
    store.set_subscription("type", filters={"asset_types": ["CIDR"]})
    change(store)
    assert {item["chat_id"] for item in store.pending_deliveries(200)} == {"all"}
    store.set_subscription("all", active=False)
    assert store.pending_deliveries(200) == []
    store.set_subscription("all", active=True)
    assert store.pending_deliveries(200) == []


def test_delete_subscription_erases_chat_data(store):
    baseline(store)
    store.set_subscription("user-to-delete", filters={"handles": ["example"]})
    change(store)
    store.queue_text("user-to-delete", "hello", "reply")
    store.delete_subscription("user-to-delete")
    assert store.get_subscription("user-to-delete") is None
    assert store.status(200)["pending_deliveries"] == 0
    assert store.pending_deliveries(200) == []


def test_scheduler_priority_and_status(store):
    value = baseline(store)
    assert store.next_due(150) is None
    assert store.next_due(161) == value["program"]
    store.defer_program("1", 500)
    assert store.next_due(200) is None
    changed = deepcopy(value["program"])
    changed["policy"] = "Changed policy"
    store.sync_catalog([changed], 200)
    assert store.next_due(200) == changed
    status = store.status(200)
    assert status["active_programs"] == status["baselined_programs"] == 1
    assert status["oldest_snapshot_age_seconds"] == 99
    assert status["last_scope_at"] == 101


def test_backup_and_retention_preserve_unsent_events(store, tmp_path):
    baseline(store)
    store.set_subscription("1")
    change(store)
    store.prune(10000000, retention_days=1)
    assert store.status(10000000)["event_count"] == 1
    backup = tmp_path / "backup.db"
    store.backup(backup)
    copied = Store(backup)
    assert copied.get_program("example") == store.get_program("example")
    assert copied.pending_deliveries(10000000)
    copied.close()
    while pending := store.pending_deliveries(10000000):
        for item in pending:
            store.delivered(item["id"], 500, 201)
    store.prune(10000000, retention_days=1)
    assert store.status(10000000)["event_count"] == 0
    assert store.get_program("example")


def test_future_schema_refused(tmp_path):
    path = tmp_path / "newer.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported"):
        Store(path)


def test_unknown_program_and_mismatched_handle_refused(store):
    baseline(store)
    for value in (snapshot("2", "other"), snapshot("1", "renamed")):
        with pytest.raises(ValueError):
            store.apply_snapshot(value, 200, 60)


def test_event_json_is_valid_and_retrievable(store):
    baseline(store)
    _, events = change(store)
    assert json.loads(json.dumps(store.recent_events())) == events
    assert store.recent_events(handle="other") == []


def test_new_program_priority_does_not_starve_baseline(store):
    old = [snapshot(str(index), f"existing_{index}") for index in range(1, 4)]
    store.sync_catalog([item["program"] for item in old], 100)
    new = [snapshot(str(index), f"new_{index}") for index in range(4, 11)]
    store.sync_catalog([item["program"] for item in old + new], 200)
    for _ in range(5):
        program = store.next_due(200)
        assert program["handle"].startswith("new_")
        value = next(item for item in new if item["program"]["id"] == program["id"])
        store.apply_snapshot(value, 200, 60)
    assert store.next_due(200)["handle"].startswith("existing_")
    store.apply_snapshot(old[0], 200, 60)
    assert store.next_due(200)["handle"].startswith("new_")


def test_pause_cancels_dead_alerts_so_command_replies_can_resume(store):
    baseline(store)
    store.set_subscription("1")
    change(store)
    delivery = store.pending_deliveries(200)[0]
    store.delivery_failed(delivery["id"], 200, permanent=True)
    store.queue_text("1", "paused", "pause")
    store.set_subscription("1", active=False)
    assert [item["text"] for item in store.pending_deliveries(200)] == ["paused"]
    assert store.retry_dead() == 0


def test_detail_visibility_revocation_cancels_pending_and_dead_deliveries(store):
    baseline(store)
    store.set_subscription("pending")
    store.set_subscription("dead")
    change(store)
    first = next(item for item in store.pending_deliveries(200) if item["chat_id"] == "dead")
    store.delivery_failed(first["id"], 200, permanent=True)
    store.deactivate_program("1", 201)
    assert store.get_program("example") is None
    assert store.recent_events() == []
    assert store.pending_deliveries(202) == []
    assert store.status(202)["pending_deliveries"] == 0
    assert store.status(202)["dead_deliveries"] == 0
    assert store.retry_dead() == 0


def test_command_reply_bypasses_stalled_event_without_reordering_event_parts(
    store, legacy_multipart_renderer
):
    baseline(store)
    store.set_subscription("1")
    value = snapshot()
    value["scopes"][0]["instruction"] = "Changed legacy instructions"
    store.apply_snapshot(value, 200, 60)
    first = store.pending_deliveries(200)[0]
    store.delivery_failed(first["id"], 200, retry_after=30)
    store.queue_text("1", "Source status", "command:20")
    ready = store.pending_deliveries(201)
    assert [item["text"] for item in ready] == ["Source status"]
    store.delivered(ready[0]["id"], 1, 201)
    assert store.pending_deliveries(229) == []
    ready = store.pending_deliveries(230)
    assert ready[0]["id"] == first["id"]
    store.delivered(first["id"], 2, 230)
    assert store.pending_deliveries(230)[0]["part"] == 2


def test_dead_command_does_not_block_future_replies(store):
    store.queue_text("1", "old reply", "command:1")
    first = store.pending_deliveries(100)[0]
    store.delivery_failed(first["id"], 100, permanent=True)
    store.queue_text("1", "current status", "command:2")
    ready = store.pending_deliveries(101)
    assert [item["text"] for item in ready] == ["current status"]
    store.delivered(ready[0]["id"], 1, 101)
    assert store.retry_dead() == 1
    assert store.pending_deliveries(102)[0]["id"] == first["id"]


def test_dead_event_blocks_its_remaining_parts_but_not_later_events(
    store, legacy_multipart_renderer
):
    baseline(store)
    store.set_subscription("1")
    value = snapshot()
    value["scopes"][0]["instruction"] = "Changed legacy instructions"
    first_event = store.apply_snapshot(value, 200, 60)[0]
    first_part = store.pending_deliveries(200)[0]
    store.delivery_failed(first_part["id"], 200, permanent=True)
    assert store.pending_deliveries(201) == []
    value["scopes"].append({"id": "11", "asset_identifier": "new.example.com", "asset_type": "URL"})
    later_event = store.apply_snapshot(value, 202, 60)[0]
    ready = store.pending_deliveries(203)
    assert ready
    assert all(item["event_id"] == later_event["id"] for item in ready)
    store.delivered(ready[0]["id"], 1, 203)
    assert store.retry_dead() == 1
    ready = store.pending_deliveries(204)
    assert ready[0]["id"] == first_part["id"]
    store.delivered(first_part["id"], 2, 204)
    ready = store.pending_deliveries(204)
    assert ready[0]["event_id"] == first_event["id"]
    assert ready[0]["part"] == 2


def test_stale_event_filter_applies_before_limit_so_later_commands_do_not_starve(store):
    baseline(store)
    for number in range(150):
        store.set_subscription(f"subscriber-{number}")
    change(store)
    store.queue_text("operator", "status remains available", "command:1")
    assert len(store.pending_deliveries(201, limit=100)) == 100
    assert store.pending_deliveries(201, limit=100)[0]["chat_id"] == "operator"
    ready = store.pending_deliveries(201, limit=100, include_events=False)
    assert [(item["chat_id"], item["text"]) for item in ready] == [
        ("operator", "status remains available")
    ]


def test_failed_priority_snapshots_still_yield_to_ordinary_refresh(store):
    old = snapshot()
    store.sync_catalog([old["program"]], 100)
    new = [snapshot(str(index), f"new_{index}") for index in range(2, 10)]
    store.sync_catalog([old["program"]] + [item["program"] for item in new], 200)
    for _ in range(5):
        program = store.next_due(200)
        assert program["handle"].startswith("new_")
        store.defer_program(program["id"], 500)
    assert store.get_meta("priority_streak") == "5"
    assert store.next_due(200) == old["program"]
    store.defer_program(old["program"]["id"], 500)
    assert store.get_meta("priority_streak") == "0"
    assert store.next_due(200)["handle"].startswith("new_")


def test_command_parts_take_precedence_over_ready_legacy_event_parts(
    store, legacy_multipart_renderer
):
    baseline(store)
    store.set_subscription("1")
    value = snapshot()
    value["scopes"][0]["instruction"] = "Changed legacy instructions"
    event = store.apply_snapshot(value, 200, 60)[0]
    store.queue_text("1", "status part one", "command:1:1")
    store.queue_text("1", "status part two", "command:1:2")
    for index, expected in enumerate(("status part one", "status part two"), 1):
        ready = store.pending_deliveries(201, limit=1)
        assert ready[0]["event_id"] is None
        assert ready[0]["text"] == expected
        store.delivered(ready[0]["id"], index, 201)
    ready = store.pending_deliveries(201, limit=1)
    assert ready[0]["event_id"] == event["id"] and ready[0]["part"] == 1
    store.delivered(ready[0]["id"], 3, 201)
    ready = store.pending_deliveries(201, limit=1)
    assert ready[0]["event_id"] == event["id"] and ready[0]["part"] == 2


def test_baseline_complete_distinguishes_uninitialized_partial_and_completed_catalog(store):
    assert store.status(100)["baseline_complete"] is False
    first, second = snapshot(), snapshot("2", "second")
    store.sync_catalog([first["program"], second["program"]], 100)
    assert store.status(100)["baseline_complete"] is False
    store.apply_snapshot(first, 101, 60)
    assert store.status(101)["baseline_complete"] is False
    store.apply_snapshot(second, 102, 60)
    assert store.status(102)["baseline_complete"] is True
    store.sync_catalog([], 103)
    assert store.status(103)["baseline_complete"] is True


def test_pending_age_uses_oldest_pending_only_and_requires_no_schema_change(store, monkeypatch):
    version = store._db.execute("PRAGMA user_version").fetchone()[0]
    assert store.status(100)["pending_delivery_age_seconds"] is None
    monkeypatch.setattr("tower.store.time.time", lambda: 100)
    store.queue_text("first", "older", "1")
    monkeypatch.setattr("tower.store.time.time", lambda: 125)
    store.queue_text("second", "newer", "2")
    assert store.status(150)["pending_delivery_age_seconds"] == 50
    older = store.pending_deliveries(150)[0]
    store.delivered(older["id"], 1, 150)
    assert store.status(150)["pending_delivery_age_seconds"] == 25
    newer = store.pending_deliveries(150)[0]
    store.delivery_failed(newer["id"], 150, permanent=True)
    assert store.status(150)["pending_delivery_age_seconds"] is None
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == version


def test_avatar_only_catalog_update_preserves_rotation_without_alerts(store):
    value = snapshot()
    value["program"]["profile_picture"] = "https://images.example.test/avatar?signature=old"
    store.set_subscription("1")
    store.sync_catalog([value["program"]], 100)
    assert store.apply_snapshot(value, 101, 60) == []
    value["program"]["profile_picture"] = "https://images.example.test/avatar?signature=renewed"
    store.sync_catalog([value["program"]], 150)
    assert store.next_due(150) is None
    assert store.next_due(161) == value["program"]
    assert store.apply_snapshot(value, 161, 60) == []
    assert (
        store.get_program("example")["program"]["profile_picture"]
        == value["program"]["profile_picture"]
    )
    assert store.recent_events() == []
    assert store.pending_deliveries(161) == []
    # A real policy change still expedites collection despite simultaneous image churn.
    value["program"]["profile_picture"] = "https://images.example.test/avatar?signature=newer"
    value["program"]["policy"] = "Updated policy"
    value["scopes"][0]["instruction"] = "Updated scope rules"
    store.sync_catalog([value["program"]], 170)
    assert store.next_due(170) == value["program"]
    events = store.apply_snapshot(value, 170, 60)
    assert [event["kind"] for event in events] == ["program_updated", "scope_updated"]
    assert store.pending_deliveries(170)


def test_recent_hides_legacy_cosmetic_events_before_limit_without_changing_audit(tmp_path):
    path = tmp_path / "legacy-history.db"
    store = Store(path)
    first, other = snapshot(), snapshot("2", "other")
    store.set_subscription("1")
    store.sync_catalog([first["program"], other["program"]], 100)
    store.apply_snapshot(first, 101, 60)
    store.apply_snapshot(other, 102, 60)
    first["program"]["policy"] = "Meaningful policy change"
    policy_event = store.apply_snapshot(first, 200, 60)[0]
    first["scopes"][0]["instruction"] = "Meaningful scope change"
    scope_event = store.apply_snapshot(first, 201, 60)[0]
    other["program"]["policy"] = "Now unavailable program"
    store.apply_snapshot(other, 202, 60)
    store.deactivate_program("2", 203)
    # Re-create rows emitted by the previous image-sensitive diff implementation.
    # The current implementation must retain these rows and their delivery audit.
    with store._db:
        for index in range(13):
            event = deepcopy(policy_event)
            event["id"] = f"legacy-cosmetic-{index}"
            keys = ("profile_picture",) if index % 3 == 0 else ("updated_at",)
            if index % 3 == 2:
                keys = ("profile_picture", "updated_at")
            event["changes"] = {key: {"before": "old", "after": "renewed"} for key in keys}
            event["observed_at"] = f"1970-01-01T00:05:{index:02d}Z"
            store._db.execute(
                "INSERT INTO events(id,program_id,handle,observed_at,event_json) VALUES(?,?,?,?,?)",
                (event["id"], "1", "example", 300 + index, json.dumps(event)),
            )
            store._db.execute(
                """INSERT INTO deliveries(dedup_key,event_id,chat_id,text,state,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    event["id"],
                    event["id"],
                    "1",
                    "Legacy delivery audit",
                    "cancelled",
                    300 + index,
                    400,
                ),
            )

    def audit_rows(database):
        return (
            [tuple(row) for row in database.execute("SELECT * FROM events ORDER BY id")],
            [tuple(row) for row in database.execute("SELECT * FROM deliveries ORDER BY id")],
        )

    before = audit_rows(store._db)
    expected = [scope_event["id"], policy_event["id"]]
    assert [event["id"] for event in store.recent_events(limit=2)] == expected
    assert [event["id"] for event in store.recent_events(limit=1)] == expected[:1]
    assert [event["id"] for event in store.recent_events(limit=2, handle="example")] == expected
    assert store.recent_events(handle="other") == []
    assert store.recent_events(handle="unknown") == []
    assert audit_rows(store._db) == before
    assert store.status(500)["event_count"] == 16
    store.close()
    reopened = Store(path)
    try:
        assert [event["id"] for event in reopened.recent_events(limit=2)] == expected
        assert audit_rows(reopened._db) == before
    finally:
        reopened.close()


def test_recent_keeps_legacy_policy_changes_that_also_contain_avatar_changes(store):
    value = baseline(store)
    value["program"]["policy"] = "Meaningful policy change"
    event = store.apply_snapshot(value, 200, 60)[0]
    event["changes"]["profile_picture"] = {"before": "old", "after": "renewed"}
    event["changes"]["updated_at"] = {"before": "old date", "after": "new date"}
    with store._db:
        store._db.execute(
            "UPDATE events SET event_json=? WHERE id=?", (json.dumps(event), event["id"])
        )
    assert store.recent_events(limit=1) == [event]


def legacy_database(path, version):
    """Create a prior schema, including durable delivery states."""
    priority = ", priority INTEGER NOT NULL DEFAULT 0" if version >= 2 else ""
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE programs(
                id TEXT PRIMARY KEY, handle TEXT NOT NULL, catalog_json TEXT NOT NULL,
                snapshot_json TEXT, first_seen_at REAL NOT NULL, last_snapshot_at REAL,
                active INTEGER NOT NULL, silent INTEGER NOT NULL, due_at REAL NOT NULL
            """
            + priority
            + """
            );
            CREATE TABLE events(
                id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES programs(id),
                handle TEXT NOT NULL, observed_at REAL NOT NULL, event_json TEXT NOT NULL
            );
            CREATE TABLE subscriptions(
                chat_id TEXT PRIMARY KEY, active INTEGER NOT NULL, filters_json TEXT NOT NULL
            );
            CREATE TABLE deliveries(
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT UNIQUE NOT NULL,
                event_id TEXT REFERENCES events(id), chat_id TEXT NOT NULL, text TEXT NOT NULL,
                part INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, finished_at REAL, message_id INTEGER
            );
            """
        )
        if version >= 3:
            database.execute("ALTER TABLE deliveries ADD COLUMN reply_markup_json TEXT")
        value = snapshot()
        database.execute("INSERT INTO meta VALUES('catalog_initialized','1')")
        database.execute("INSERT INTO meta VALUES('telegram_offset','51')")
        database.execute("INSERT INTO meta VALUES('priority_streak','4')")
        database.execute(
            """INSERT INTO programs(id,handle,catalog_json,snapshot_json,first_seen_at,
            last_snapshot_at,active,silent,due_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("1", "example", json.dumps(value["program"]), json.dumps(value), 100, 101, 1, 0, 200),
        )
        if version >= 2:
            database.execute("UPDATE programs SET priority=2")
        database.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (
                "legacy-event",
                "1",
                "example",
                110,
                json.dumps({"id": "legacy-event", "kind": "scope_added"}),
            ),
        )
        database.execute(
            "INSERT INTO subscriptions VALUES(?,?,?)",
            ("123", 1, json.dumps({"handles": [], "asset_types": [], "bounties_only": False})),
        )
        database.executemany(
            """INSERT INTO deliveries(id,dedup_key,event_id,chat_id,text,part,attempts,
            state,next_attempt_at,created_at,finished_at,message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    1,
                    "event:legacy:123:1",
                    "legacy-event",
                    "123",
                    "part one",
                    1,
                    2,
                    "pending",
                    250,
                    100,
                    None,
                    None,
                ),
                (
                    2,
                    "event:legacy:123:2",
                    "legacy-event",
                    "123",
                    "part two",
                    2,
                    0,
                    "pending",
                    0,
                    100,
                    None,
                    None,
                ),
                (3, "reply:456:dead", None, "456", "dead reply", 1, 4, "dead", 500, 90, 150, None),
                (
                    4,
                    "reply:789:done",
                    None,
                    "789",
                    "delivered reply",
                    1,
                    0,
                    "delivered",
                    0,
                    80,
                    130,
                    55,
                ),
            ],
        )
        if version >= 3:
            database.execute(
                "UPDATE deliveries SET reply_markup_json=? WHERE id=3",
                (json.dumps({"keyboard": [["Help"]], "resize_keyboard": True}),),
            )
        database.execute(f"PRAGMA user_version={version}")


def legacy_rows(database):
    """Snapshot old columns only so a nullable additive migration can be compared."""
    database.row_factory = sqlite3.Row
    result = {}
    for table in ("meta", "programs", "events", "subscriptions", "deliveries"):
        rows = []
        for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            value = dict(row)
            value.pop("reply_markup_json", None)
            value.pop("priority", None)
            rows.append(value)
        result[table] = rows
    return result


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_legacy_schema_migrates_atomically_preserving_delivery_state_and_restart(tmp_path, version):
    path = tmp_path / f"legacy-v{version}.db"
    legacy_database(path, version)
    with sqlite3.connect(path) as database:
        before = legacy_rows(database)
    store = Store(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert store._db.execute("SELECT COUNT(*) FROM program_failures").fetchone()[0] == 0
    assert legacy_rows(store._db) == before
    assert store.get_meta("telegram_offset") == "51"
    assert store._db.execute("SELECT priority FROM programs").fetchone()[0] == (
        2 if version >= 2 else 0
    )
    assert store.pending_deliveries(249) == []
    ready = store.pending_deliveries(250)
    assert [(item["id"], item["part"], item["attempts"]) for item in ready] == [(1, 1, 2)]
    assert ready[0]["reply_markup"] is None
    assert "reply_markup_json" not in ready[0]
    store.close()
    reopened = Store(path)
    try:
        assert legacy_rows(reopened._db) == before
        assert (
            reopened._db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='deliveries'"
            ).fetchone()[0]
            == 4
        )
        reopened.delivered(1, 70, 251)
        assert reopened.pending_deliveries(251)[0]["part"] == 2
        assert reopened.retry_dead() == 1
        assert reopened.pending_deliveries(251)[0]["id"] == 3
        if version >= 3:
            assert reopened.pending_deliveries(251)[0]["reply_markup"] == {
                "keyboard": [["Help"]],
                "resize_keyboard": True,
            }
        reopened.queue_text("999", "new message", "new")
        assert (
            reopened._db.execute("SELECT id FROM deliveries WHERE chat_id='999'").fetchone()[0] == 5
        )
        reopened.queue_text("456", "duplicate should not overwrite", "dead")
        assert reopened.pending_deliveries(251)[0]["text"] == "dead reply"
    finally:
        reopened.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_failed_migration_rolls_back_columns_version_and_existing_rows(
    tmp_path, monkeypatch, version
):
    path = tmp_path / f"rollback-v{version}.db"
    legacy_database(path, version)
    connect = sqlite3.connect
    with connect(path) as database:
        before = legacy_rows(database)
        old_schema = [
            tuple(row)
            for row in database.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
        ]

    class FailVersionUpdate(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA user_version=4":
                raise RuntimeError("simulated migration interruption")
            return super().execute(sql, parameters)

    def failing_connect(*args, **kwargs):
        kwargs["factory"] = FailVersionUpdate
        return connect(*args, **kwargs)

    monkeypatch.setattr("tower.store.sqlite3.connect", failing_connect)
    with pytest.raises(RuntimeError, match="migration interruption"):
        Store(path)
    with connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == version
        assert legacy_rows(database) == before
        assert [
            tuple(row)
            for row in database.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
        ] == old_schema
        if version < 3:
            assert "reply_markup_json" not in {
                row[1] for row in database.execute("PRAGMA table_info(deliveries)")
            }
        if version == 1:
            assert "priority" not in {
                row[1] for row in database.execute("PRAGMA table_info(programs)")
            }


def test_keyboard_metadata_is_normalized_durable_deduplicated_and_not_mutably_shared(tmp_path):
    path = tmp_path / "keyboard.db"
    store = Store(path)
    keyboard = {"keyboard": [[{"text": "Help"}, {"text": "Status"}]], "resize_keyboard": True}
    expected = {"keyboard": [["Help", "Status"]], "resize_keyboard": True}
    assert store.queue_text("123", "original", "reply", reply_markup=keyboard) is None
    keyboard["keyboard"][0][0]["text"] = "changed after queue"
    store.queue_text("123", "replacement", "reply", reply_markup={"keyboard": [["Other"]]})
    first = store.pending_deliveries(100)[0]
    assert first["text"] == "original" and first["reply_markup"] == expected
    assert "reply_markup_json" not in first
    first["reply_markup"]["keyboard"][0][0] = "changed after read"
    store.delivery_failed(first["id"], 100, retry_after=30)
    store.close()
    reopened = Store(path)
    try:
        assert reopened.pending_deliveries(129) == []
        retried = reopened.pending_deliveries(130)[0]
        assert retried["reply_markup"] == expected
        assert retried["attempts"] == 1
        assert reopened.status(130)["pending_deliveries"] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("chat_id", ["-100123", "@channel", "0", "00123", "１２３", "named-chat"])
def test_keyboard_metadata_rejects_nonprivate_destinations_without_queueing(store, chat_id):
    with pytest.raises(ValueError, match="private chat"):
        store.queue_text(chat_id, "menu", "reply", reply_markup={"keyboard": [["Help"]]})
    assert store.status(100)["pending_deliveries"] == 0


@pytest.mark.parametrize(
    "markup",
    [
        {"inline_keyboard": [[{"text": "unsafe", "url": "https://example.invalid"}]]},
        {"keyboard": [[{"text": "unsafe", "request_contact": True}]]},
        {"keyboard": [["Help"]], "resize_keyboard": "yes"},
        {"keyboard": []},
    ],
)
def test_invalid_keyboard_metadata_never_enters_outbox(store, markup):
    with pytest.raises(ValueError):
        store.queue_text("123", "menu", "reply", reply_markup=markup)
    assert store.status(100)["pending_deliveries"] == 0


def test_legacy_text_and_event_alerts_have_no_keyboard_and_erasure_removes_keyboard_data(store):
    baseline(store)
    store.set_subscription("123")
    change(store)
    event = store.pending_deliveries(200)[0]
    assert event["reply_markup"] is None and "reply_markup_json" not in event
    store.queue_text("@channel", "plain message", "plain")
    plain = next(item for item in store.pending_deliveries(200) if item["chat_id"] == "@channel")
    assert plain["reply_markup"] is None
    store.queue_text("123", "menu", "menu", reply_markup={"keyboard": [["Help"]]})
    store.delete_subscription("123")
    assert (
        store._db.execute("SELECT COUNT(*) FROM deliveries WHERE chat_id='123'").fetchone()[0] == 0
    )
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE reply_markup_json IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
