"""Bounded concurrent collection without duplicate work or persistent claims."""

import copy
import threading
import time
from contextlib import closing

import httpx
import pytest

from tower.config import Settings
from tower.service import Monitor
from tower.source import HackerOneClient, SourceError
from tower.store import Store


def snapshot(identifier):
    return {
        "program": {
            "id": str(identifier),
            "handle": f"fixture_{identifier}",
            "name": f"Fixture {identifier}",
            "state": "public_mode",
            "offers_bounties": True,
        },
        "scopes": [
            {
                "id": "10",
                "asset_identifier": f"asset-{identifier}.example.test",
                "asset_type": "URL",
                "eligible_for_submission": True,
                "eligible_for_bounty": True,
            }
        ],
        "exclusions": [],
    }


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "scheduler.sqlite3")) as database:
        yield database


def baseline(store, values):
    store.sync_catalog([value["program"] for value in values], 1000)
    for value in values:
        store.apply_snapshot(value, 1000, 60)


def start_tick(monitor):
    errors = []

    def run():
        try:
            monitor.scope_tick()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def finish(thread, errors):
    thread.join(3)
    assert not thread.is_alive(), "Collection thread leaked"
    assert errors == []


def test_next_due_exclusions_preserve_priority_and_fairness(store):
    old = [snapshot(1), snapshot(2)]
    baseline(store, old)
    new = snapshot(3)
    store.sync_catalog([value["program"] for value in [*old, new]], 1060)
    assert store.next_due(1060)["id"] == "3"
    assert store.next_due(1060, exclude_ids=("3",))["id"] == "1"
    store.set_meta("priority_streak", "5")
    assert store.next_due(1060, exclude_ids=("1",))["id"] == "2"
    assert store.next_due(1060, exclude_ids=("1", "2"))["id"] == "3"
    assert store.next_due(1060, exclude_ids=("1", "2", "3")) is None
    assert store.next_due(1060, exclude_ids=("x') OR 1=1 --",))["id"] == "1"


def test_two_workers_never_collect_same_program_or_exceed_limit(store):
    values = [snapshot(1), snapshot(2), snapshot(3)]
    baseline(store, values)
    store.set_subscription("100")
    for value in values:
        value["scopes"][0]["instruction"] = "Updated public instructions"
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    class BlockingSource:
        def fetch_snapshot(self, program):
            with calls_lock:
                calls.append(program["id"])
            entered.wait(3)
            assert release.wait(3)
            return copy.deepcopy(values[int(program["id"]) - 1])

    monitor = Monitor(store, BlockingSource(), Settings(), clock=lambda: 1060)
    workers = [start_tick(monitor) for _ in range(2)]
    try:
        entered.wait(3)
        monitor.scope_tick()  # A third caller cannot add another in-flight request.
        assert sorted(calls) == ["1", "2"]
        assert store.recent_events() == []
    finally:
        release.set()
        for thread, errors in workers:
            finish(thread, errors)
    assert len(store.recent_events()) == 2
    assert store.status(1060)["pending_deliveries"] == 2
    assert store.next_due(1060)["id"] == "3"


def test_large_collection_does_not_block_a_new_program_or_leak_partial_data(store):
    large = snapshot(1)
    baseline(store, [large])
    entered = threading.Event()
    release = threading.Event()
    new = snapshot(2)

    class Source:
        def fetch_snapshot(self, program):
            if program["id"] == "1":
                entered.set()
                assert release.wait(3)
                return copy.deepcopy(large)
            return copy.deepcopy(new)

    monitor = Monitor(store, Source(), Settings(), clock=lambda: 1060)
    thread, errors = start_tick(monitor)
    try:
        assert entered.wait(3)
        store.sync_catalog([large["program"], new["program"]], 1060)
        monitor.scope_tick()
        events = store.recent_events()
        assert len(events) == 1
        assert events[0]["kind"] == "program_discovered"
        assert events[0]["program_id"] == "2"
        assert store.get_program("fixture_1") == large
    finally:
        release.set()
        finish(thread, errors)


def test_single_worker_configuration_keeps_second_caller_idle(store):
    values = [snapshot(1), snapshot(2)]
    baseline(store, values)
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Source:
        def fetch_snapshot(self, program):
            calls.append(program["id"])
            entered.set()
            assert release.wait(3)
            return copy.deepcopy(values[int(program["id"]) - 1])

    monitor = Monitor(store, Source(), Settings(scope_workers=1), clock=lambda: 1060)
    thread, errors = start_tick(monitor)
    try:
        assert entered.wait(3)
        monitor.scope_tick()
        assert calls == ["1"]
    finally:
        release.set()
        finish(thread, errors)


def test_parallel_workers_use_the_same_structured_scope_request_budget(store):
    values = [snapshot(1), snapshot(2)]
    baseline(store, values)
    scopes_requested = []
    request_lock = threading.Lock()

    def handler(request):
        assert request.url.host == "api.hackerone.com"
        segments = request.url.path.split("/")
        identifier = int(segments[4].rsplit("_", 1)[1])
        value = values[identifier - 1]
        if request.url.path.endswith("/structured_scopes"):
            with request_lock:
                scopes_requested.append(time.monotonic())
            entry = value["scopes"][0]
            payload = {
                "data": [{"id": entry["id"], "type": "structured-scope", "attributes": entry}]
            }
        elif request.url.path.endswith("/scope_exclusions"):
            payload = {"data": []}
        else:
            program = value["program"]
            payload = {"id": program["id"], "type": "program", "attributes": program}
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        source = HackerOneClient("fixture", "fake-token", client=client)
        monitor = Monitor(store, source, Settings(), clock=lambda: 1060)
        workers = [start_tick(monitor) for _ in range(2)]
        for thread, errors in workers:
            finish(thread, errors)
    assert len(scopes_requested) == 2
    # Two independent buckets would issue both requests together. The shared
    # 40/minute budget spaces them by 1.5s; tolerate scheduler measurement noise.
    assert abs(scopes_requested[1] - scopes_requested[0]) >= 1.3
    assert store.status(1060)["last_scope_at"] == 1060


@pytest.mark.parametrize("failure", [SourceError("temporary failure"), ValueError("rejected")])
def test_claim_released_after_expected_error_and_retry_preserves_snapshot(store, failure):
    value = snapshot(1)
    baseline(store, [value])
    now = [1060]

    class Source:
        calls = 0

        def fetch_snapshot(self, program):
            self.calls += 1
            if self.calls == 1:
                raise failure
            return copy.deepcopy(value)

    source = Source()
    monitor = Monitor(store, source, Settings(), clock=lambda: now[0])
    monitor.scope_tick()
    assert store.get_program("fixture_1") == value
    assert store.next_due(1060) is None
    now[0] = 1120
    monitor.scope_tick()
    assert source.calls == 2
    assert store.status(now[0])["last_scope_at"] == now[0]
    assert store.recent_events() == []


def test_fatal_collection_error_also_releases_claim(store):
    value = snapshot(1)
    baseline(store, [value])

    class Source:
        fail = True

        def fetch_snapshot(self, program):
            if self.fail:
                raise RuntimeError("internal failure")
            return copy.deepcopy(value)

    source = Source()
    monitor = Monitor(store, source, Settings(), clock=lambda: 1060)
    with pytest.raises(RuntimeError, match="internal failure"):
        monitor.scope_tick()
    source.fail = False
    monitor.scope_tick()
    assert store.status(1060)["last_scope_at"] == 1060


def test_parallel_cooldowns_never_shorten_an_existing_longer_cooldown(store):
    values = [snapshot(1), snapshot(2)]
    baseline(store, values)
    entered = {"1": threading.Event(), "2": threading.Event()}
    release = {"1": threading.Event(), "2": threading.Event()}

    class Source:
        def fetch_snapshot(self, program):
            identifier = program["id"]
            entered[identifier].set()
            assert release[identifier].wait(3)
            raise SourceError(
                "rate limit", retry_after=300 if identifier == "1" else 60, status_code=429
            )

    monitor = Monitor(store, Source(), Settings(), clock=lambda: 1060)
    workers = [start_tick(monitor)]
    try:
        assert entered["1"].wait(3)
        workers.append(start_tick(monitor))
        assert entered["2"].wait(3)
        release["1"].set()
        finish(*workers[0])
        assert monitor.cooldown == 1360
    finally:
        for signal in release.values():
            signal.set()
        for thread, errors in workers:
            finish(thread, errors)
    assert monitor.cooldown == 1360


def test_catalog_failure_preserves_longer_scope_cooldown(store):
    class Source:
        def list_programs(self):
            monitor._extend_cooldown(1600)
            raise SourceError("shorter server retry", retry_after=60, status_code=429)

    monitor = Monitor(store, Source(), Settings(), clock=lambda: 1060)
    monitor.catalog_tick()
    assert monitor.cooldown == monitor.catalog_due == 1600


def test_visibility_change_during_claim_discards_snapshot_and_alerts(store):
    value = snapshot(1)
    baseline(store, [value])
    store.set_subscription("100")
    changed = copy.deepcopy(value)
    changed["scopes"][0]["instruction"] = "New instruction"
    entered, release = threading.Event(), threading.Event()

    class Source:
        def fetch_snapshot(self, program):
            entered.set()
            assert release.wait(3)
            return changed

    monitor = Monitor(store, Source(), Settings(), clock=lambda: 1060)
    thread, errors = start_tick(monitor)
    try:
        assert entered.wait(3)
        store.sync_catalog([], 1060)
    finally:
        release.set()
        finish(thread, errors)
    assert store.get_program("fixture_1") is None
    assert store.recent_events() == []
    assert store.status(1060)["pending_deliveries"] == 0


def test_restart_during_collection_retries_uncommitted_work_exactly_once(store, tmp_path):
    value = snapshot(1)
    baseline(store, [value])
    store.set_subscription("100")
    changed = copy.deepcopy(value)
    changed["scopes"][0]["instruction"] = "New instruction"
    entered, release = threading.Event(), threading.Event()

    class Source:
        def fetch_snapshot(self, program):
            entered.set()
            assert release.wait(3)
            return copy.deepcopy(changed)

    monitor = Monitor(store, Source(), Settings(), clock=lambda: 1060)
    thread, errors = start_tick(monitor)
    backup = tmp_path / "restart.sqlite3"
    try:
        assert entered.wait(3)
        # This consistent copy represents the durable state before a process exit.
        store.backup(backup)
    finally:
        release.set()
        finish(thread, errors)
    with closing(Store(backup)) as restarted:
        assert restarted.next_due(1060)["id"] == "1"
        retry = Monitor(restarted, Source(), Settings(), clock=lambda: 1060)
        retry.scope_tick()
        assert len(restarted.recent_events()) == 1
        assert restarted.status(1060)["pending_deliveries"] == 1
        assert restarted.apply_snapshot(changed, 1120, 60) == []
        assert restarted.status(1120)["pending_deliveries"] == 1


@pytest.mark.parametrize("workers", [0, 3, True, 1.5])
def test_direct_monitor_configuration_cannot_bypass_worker_bound(store, workers):
    with pytest.raises(ValueError, match="Scope worker count"):
        Monitor(store, object(), Settings(scope_workers=workers))
