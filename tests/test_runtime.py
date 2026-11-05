"""Real SQLite persistence with simulated source and Telegram services."""

import logging
import threading
import time
from copy import deepcopy

import pytest

from tower.config import Settings
from tower.keyboards import MAIN_KEYBOARD
from tower.service import LOG, Dispatcher, Monitor, Service, configure_logging
from tower.source import SourceError
from tower.store import Store
from tower.telegram import TelegramError


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def snapshot(program_id="1", handle="example"):
    return {
        "program": {
            "id": program_id,
            "handle": handle,
            "state": "public_mode",
            "name": handle,
            "offers_bounties": True,
        },
        "scopes": [{"id": "10", "asset_identifier": "example.com", "asset_type": "URL"}],
        "exclusions": [],
    }


class FakeSource:
    def __init__(self):
        self.snapshots = {"1": snapshot()}
        self.catalog_error = None
        self.snapshot_error = None
        self.catalog_calls = 0
        self.snapshot_calls = 0

    def list_programs(self):
        self.catalog_calls += 1
        if self.catalog_error:
            raise self.catalog_error
        return deepcopy([value["program"] for value in self.snapshots.values()])

    def fetch_snapshot(self, program):
        self.snapshot_calls += 1
        if self.snapshot_error:
            raise self.snapshot_error
        return deepcopy(self.snapshots[program["id"]])


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.markups = []
        self.errors = []
        self.attempts = 0

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return len(self.sent)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "runtime.db")
    yield value
    value.close()


def monitor(store, source, clock):
    return Monitor(store, source, Settings(catalog_interval=120, scope_refresh=60), clock)


def test_monitor_baseline_changes_discovery_restart_and_delivery(tmp_path):
    path = tmp_path / "restart.db"
    store = Store(path)
    source, clock, telegram = FakeSource(), Clock(), FakeTelegram()
    store.set_subscription("channel")
    worker = monitor(store, source, clock)
    worker.tick()
    assert store.status(clock())["baselined_programs"] == 1
    assert store.recent_events() == []
    source.snapshots["1"]["scopes"].append(
        {"id": "11", "asset_identifier": "new.example.com", "asset_type": "URL"}
    )
    clock.now = 1060
    worker.tick()
    assert store.recent_events()[0]["kind"] == "scope_added"
    source.snapshots["2"] = snapshot("2", "new_program")
    clock.now = 1120
    worker.tick()
    assert store.recent_events()[0]["kind"] == "program_discovered"
    assert store.status(clock())["event_count"] == 2
    store.close()
    reopened = Store(path)
    worker = monitor(reopened, source, clock)
    worker.tick()
    worker.tick()
    assert reopened.status(clock())["event_count"] == 2
    dispatcher = Dispatcher(reopened, telegram, clock=clock)
    for _ in range(50):
        dispatcher.tick()
        clock.now += 4
        if reopened.status(clock())["pending_deliveries"] == 0:
            break
    assert reopened.status(clock())["pending_deliveries"] == 0
    assert len(telegram.sent) == 2
    joined = "\n".join(text for _, text in telegram.sent)
    assert "NEW ASSET" in joined and "new.example.com" in joined
    assert "NEW PUBLIC PROGRAM FOUND" in joined and "new_program" in joined
    assert all(len(text.encode("utf-16-le")) // 2 <= 800 for _, text in telegram.sent)
    assert all(len(text.splitlines()) == 5 for _, text in telegram.sent)
    reopened.close()


def test_scope_failure_preserves_complete_snapshot_and_retries_later(store):
    source, clock = FakeSource(), Clock()
    worker = monitor(store, source, clock)
    worker.tick()
    old = store.get_program("example")
    source.snapshots["1"]["scopes"].clear()
    source.snapshot_error = SourceError("simulated source outage", status_code=503)
    clock.now = 1060
    worker.tick()
    assert store.get_program("example") == old
    assert store.recent_events() == []
    assert store.get_meta("scope_error") == "source_unavailable"
    calls = source.snapshot_calls
    clock.now = 1119
    worker.tick()
    assert source.snapshot_calls == calls
    source.snapshot_error = None
    clock.now = 1120
    worker.tick()
    assert store.recent_events()[0]["kind"] == "scope_removed"


def test_catalog_rate_limit_preserves_catalog_and_honors_cooldown(store):
    source, clock = FakeSource(), Clock()
    worker = monitor(store, source, clock)
    worker.tick()
    source.catalog_error = SourceError("rate limit", retry_after=180, status_code=429)
    clock.now = 1120
    worker.tick()
    assert store.get_meta("catalog_error") == "source_unavailable"
    assert store.status(clock())["last_catalog_at"] == 1000
    assert store.get_program("example")
    calls = source.catalog_calls
    clock.now = 1299
    worker.tick()
    assert source.catalog_calls == calls
    source.catalog_error = None
    clock.now = 1300
    worker.tick()
    assert source.catalog_calls == calls + 1
    assert store.get_meta("catalog_error") == ""


def test_scope_rate_limit_respects_global_source_cooldown(store):
    source, clock = FakeSource(), Clock()
    worker = monitor(store, source, clock)
    worker.tick()
    source.snapshot_error = SourceError("rate limit", retry_after=120, status_code=429)
    clock.now = 1060
    worker.tick()
    calls = (source.catalog_calls, source.snapshot_calls)
    clock.now = 1179
    worker.tick()
    assert (source.catalog_calls, source.snapshot_calls) == calls


def test_dispatcher_rate_limit_delays_all_sends_without_losing_messages(store):
    clock, telegram = Clock(), FakeTelegram()
    telegram.errors = [TelegramError(retry_after=30)]
    store.queue_text("one", "first", "1")
    store.queue_text("two", "independent", "2")
    dispatcher = Dispatcher(store, telegram, clock=clock)
    dispatcher.tick()
    assert telegram.attempts == 1
    assert store.status(clock())["pending_deliveries"] == 2
    clock.now = 1029
    dispatcher.tick()
    assert telegram.attempts == 1
    clock.now = 1030
    dispatcher.tick()
    dispatcher.tick()
    assert telegram.sent == [("one", "first"), ("two", "independent")]
    assert store.status(clock())["pending_deliveries"] == 0


def test_dispatcher_per_chat_spacing_and_order_allow_other_chats(store):
    clock, telegram = Clock(), FakeTelegram()
    store.queue_text("one", "part one", "1")
    store.queue_text("one", "part two", "2")
    store.queue_text("two", "independent", "3")
    dispatcher = Dispatcher(store, telegram, clock=clock)
    dispatcher.tick()
    dispatcher.tick()
    dispatcher.tick()
    assert telegram.sent == [("one", "part one"), ("two", "independent")]
    clock.now += 4
    dispatcher.tick()
    assert telegram.sent[-1] == ("one", "part two")


def test_dispatcher_dead_letter_recovers_after_configuration_fix(store):
    clock, telegram = Clock(), FakeTelegram()
    telegram.errors = [TelegramError(permanent=True)]
    store.queue_text("one", "pending message", "1")
    dispatcher = Dispatcher(store, telegram, clock=clock)
    dispatcher.tick()
    assert store.status(clock())["dead_deliveries"] == 1
    clock.now += 60
    dispatcher.tick()
    assert telegram.attempts == 1
    assert store.retry_dead() == 1
    dispatcher.tick()
    assert telegram.sent == [("one", "pending message")]


def test_stale_catalog_holds_alerts_but_same_chat_status_reply_still_arrives(store):
    source, clock, telegram = FakeSource(), Clock(), FakeTelegram()
    store.set_subscription("subscriber")
    worker = monitor(store, source, clock)
    worker.tick()
    source.snapshots["1"]["scopes"][0]["instruction"] = "Changed instructions"
    clock.now = 1060
    worker.tick()
    store.queue_text("subscriber", "status reply", "1")
    clock.now = 1700
    dispatcher = Dispatcher(store, telegram, clock=clock)
    dispatcher.tick()
    assert telegram.sent == [("subscriber", "status reply")]
    dispatcher.tick()
    assert len(telegram.sent) == 1
    clock.now += 4
    worker.tick()
    dispatcher.tick()
    assert "Instructions updated" in telegram.sent[-1][1]
    assert "example.com" in telegram.sent[-1][1]


def test_lost_public_program_is_not_sent_after_reconciliation(store):
    source, clock, telegram = FakeSource(), Clock(), FakeTelegram()
    store.set_subscription("subscriber")
    worker = monitor(store, source, clock)
    worker.tick()
    source.snapshots["1"]["scopes"][0]["instruction"] = "Changed instructions"
    clock.now = 1060
    worker.tick()
    source.snapshots.clear()
    clock.now = 1120
    worker.tick()
    Dispatcher(store, telegram, clock=clock).tick()
    assert telegram.sent == []
    assert store.status(clock())["pending_deliveries"] == 0


def test_not_public_detail_immediately_cancels_queued_alerts_before_catalog_refresh(store):
    source, clock, telegram = FakeSource(), Clock(), FakeTelegram()
    store.set_subscription("subscriber")
    worker = monitor(store, source, clock)
    worker.tick()
    source.snapshots["1"]["scopes"][0]["instruction"] = "Previously public change"
    clock.now = 1060
    worker.tick()
    assert store.status(clock())["pending_deliveries"] > 0
    source.snapshot_error = SourceError("no longer public", not_public=True)
    store.defer_program("1", 1061)
    clock.now = 1061
    worker.scope_tick()
    assert source.catalog_calls == 1
    assert store.get_program("example") is None
    assert store.status(clock())["pending_deliveries"] == 0
    Dispatcher(store, telegram, clock=clock).tick()
    assert telegram.sent == []


def test_dispatcher_delivers_commands_behind_more_than_one_hundred_stale_heads(store):
    source, clock, telegram = FakeSource(), Clock(), FakeTelegram()
    for number in range(150):
        store.set_subscription(f"subscriber-{number}")
    worker = monitor(store, source, clock)
    worker.tick()
    source.snapshots["1"]["scopes"][0]["instruction"] = "Changed instructions"
    clock.now = 1060
    worker.tick()
    store.queue_text("operator", "freshness status", "status:1")
    clock.now = 1701
    Dispatcher(store, telegram, clock=clock).tick()
    assert telegram.sent == [("operator", "freshness status")]
    assert store.status(clock())["pending_deliveries"] == 150


class LifecycleTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.polled = threading.Event()
        self.delivered = threading.Event()
        self.offsets = []

    def get_updates(self, offset, timeout=0):
        self.offsets.append(offset)
        self.polled.set()
        if len(self.offsets) == 1:
            return [
                {
                    "update_id": 41,
                    "message": {"chat": {"id": 100, "type": "private"}, "text": "/start"},
                }
            ]
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        result = super().send_message(chat_id, text, reply_markup=reply_markup)
        self.delivered.set()
        return result


class RecordingService(Service):
    """Capture actual worker threads so shutdown checks cannot miss daemon leaks."""

    def __init__(self, *args):
        super().__init__(*args)
        self.worker_threads = []
        self.thread_lock = threading.Lock()

    def _worker(self, *args):
        with self.thread_lock:
            self.worker_threads.append(threading.current_thread())
        super()._worker(*args)


def start_service(service):
    errors = []
    finished = threading.Event()

    def run():
        try:
            service.run()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors, finished


def stop_service(service, thread):
    service.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "Service runner did not stop"
    assert service.worker_threads
    assert all(not worker.is_alive() for worker in service.worker_threads), "Worker thread leaked"


@pytest.mark.parametrize("scope_workers", [1, 2])
def test_service_real_threads_process_commands_reconfigure_channel_and_stop_cleanly(
    store, scope_workers
):
    source, telegram = FakeSource(), LifecycleTelegram()
    store.set_meta("configured_channel", "old_channel")
    store.set_subscription("old_channel")
    store.set_meta("telegram_offset", "41")
    settings = Settings(channel_id="new_channel", personal_bot=True, scope_workers=scope_workers)
    service = RecordingService(settings, store, source, telegram)
    thread, errors, finished = start_service(service)
    try:
        assert telegram.delivered.wait(3), "Command reply was not delivered"
        assert telegram.markups[0] == MAIN_KEYBOARD
        assert telegram.polled.is_set()
        assert telegram.offsets[0] == 41
        assert store.get_subscription("100")["active"]
        assert store.get_subscription("old_channel") is None
        assert store.get_subscription("new_channel")["active"]
        assert store.get_meta("configured_channel") == "new_channel"
        assert store.get_meta("telegram_offset") == "42"
        assert source.cancel_event is service.stop
        assert len(service.worker_threads) == 3 + scope_workers
        if scope_workers == 2:
            assert store.get_meta("heartbeat_source_2") is not None
    finally:
        stop_service(service, thread)
    assert finished.is_set() and errors == []
    assert not service.failed.is_set()


def test_service_fatal_worker_stops_all_threads_and_reports_failure(store):
    class BrokenSource(FakeSource):
        def list_programs(self):
            raise RuntimeError("simulated internal failure")

    service = RecordingService(Settings(personal_bot=False), store, BrokenSource(), FakeTelegram())
    thread, errors, finished = start_service(service)
    try:
        assert finished.wait(3), "Fatal worker did not stop the service"
    finally:
        stop_service(service, thread)
    assert service.failed.is_set()
    assert service.stop.is_set()
    assert store.get_meta("fatal_worker") == "catalog"
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "worker stopped" in str(errors[0])
    assert "simulated internal failure" not in str(errors[0])


def test_catalog_worker_refreshes_while_scope_worker_is_blocked_and_cancels_cleanly(store):
    class BlockingSource(FakeSource):
        def __init__(self):
            super().__init__()
            self.scope_entered = threading.Event()
            self.second_catalog = threading.Event()

        def list_programs(self):
            result = super().list_programs()
            if self.catalog_calls >= 2:
                self.second_catalog.set()
            return result

        def fetch_snapshot(self, program):
            self.scope_entered.set()
            if not self.cancel_event.wait(5):
                raise RuntimeError("Test scope worker was not cancelled")
            raise SourceError("source operation cancelled")

    source = BlockingSource()
    # Direct test configuration intentionally makes each catalog worker turn due.
    service = RecordingService(
        Settings(catalog_interval=0, personal_bot=False), store, source, FakeTelegram()
    )
    thread, errors, finished = start_service(service)
    try:
        assert source.scope_entered.wait(3), "Scope worker did not begin"
        assert source.second_catalog.wait(3), "Catalog refresh was blocked by scope collection"
        assert store.get_program("example") is None
        assert source.catalog_calls >= 2
        assert len(service.worker_threads) == 4
    finally:
        stop_service(service, thread)
    assert finished.is_set() and errors == []


def test_service_maintenance_notifies_operator_when_catalog_unavailable(store):
    source, telegram = FakeSource(), LifecycleTelegram()
    source.catalog_error = SourceError("simulated temporary outage", status_code=503)
    service = RecordingService(
        Settings(personal_bot=False, admin_chat_id="999"), store, source, telegram
    )
    thread, errors, finished = start_service(service)
    try:
        assert telegram.delivered.wait(3), "Operator health notification was not delivered"
        assert telegram.sent[0][0] == "999"
        assert "Tower needs attention" in telegram.sent[0][1]
        assert store.get_meta("heartbeat") is not None
        assert store.get_meta("catalog_error") == "source_unavailable"
        assert not service.failed.is_set()
    finally:
        stop_service(service, thread)
    assert finished.is_set() and errors == []


def test_first_catalog_in_progress_does_not_send_false_outage_alert(store):
    class StartingSource(FakeSource):
        def list_programs(self):
            self.cancel_event.wait(4)
            raise SourceError("source operation cancelled")

    source, telegram = StartingSource(), LifecycleTelegram()
    service = RecordingService(
        Settings(personal_bot=False, admin_chat_id="999"), store, source, telegram
    )
    thread, errors, finished = start_service(service)
    try:
        assert not telegram.delivered.wait(1.5)
        assert store.get_meta("heartbeat") is not None
        assert store.status(time.time())["pending_deliveries"] == 0
    finally:
        stop_service(service, thread)
    assert finished.is_set() and errors == []


def test_logging_suppresses_credential_urls_and_exception_contents(store, tmp_path):
    original_handlers = list(LOG.handlers)
    original_level = LOG.level
    originals = {
        name: (logging.getLogger(name).disabled, logging.getLogger(name).level)
        for name in ("httpx", "httpcore")
    }
    directory = tmp_path / "logs"
    try:
        configure_logging(directory)
        fake_token = "123:fake-sensitive-telegram-value"
        credential_url = f"https://api.telegram.org/bot{fake_token}/sendMessage"
        logging.getLogger("httpx").info("HTTP Request: POST %s", credential_url)
        logging.getLogger("httpcore").warning("Connection failed: %s", credential_url)
        source, clock = FakeSource(), Clock()
        source.catalog_error = SourceError(credential_url)
        monitor(store, source, clock).tick()
        service = Service(Settings(personal_bot=False), store, source, FakeTelegram())

        def fail_with_secret():
            raise RuntimeError(credential_url)

        service._worker("source", fail_with_secret, 0)
        for handler in LOG.handlers:
            handler.flush()
        content = (directory / "tower.log").read_text(encoding="utf-8")
        assert "Catalog refresh failed" in content
        assert "Worker source stopped" in content
        assert fake_token not in content
        assert "api.telegram.org" not in content
    finally:
        for handler in LOG.handlers:
            handler.close()
        LOG.handlers[:] = original_handlers
        LOG.setLevel(original_level)
        for name, (disabled, level) in originals.items():
            logging.getLogger(name).disabled = disabled
            logging.getLogger(name).setLevel(level)
