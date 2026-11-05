"""Independent source, delivery and command workers backed by a durable store."""

import logging
import math
import threading
import time
from logging.handlers import RotatingFileHandler

from tower.bot import Bot
from tower.health import operator_needs_attention
from tower.source import SourceError
from tower.telegram import TelegramError

LOG = logging.getLogger("tower")


def configure_logging(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "tower.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    LOG.addHandler(handler)
    # Telegram credentials appear in URLs. Never enable HTTP request logging.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL)


class Monitor:
    def __init__(self, store, source, settings, clock=time.time):
        self.store, self.source, self.settings, self.clock = store, source, settings, clock
        self.catalog_due = 0.0
        try:
            self.cooldown = float(store.get_meta("source_cooldown_until", "0"))
            if not math.isfinite(self.cooldown) or self.cooldown < 0:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid persisted source cooldown") from None
        self._schedule_lock = threading.Lock()
        self._inflight: set[str] = set()
        if type(settings.scope_workers) is not int or not 1 <= settings.scope_workers <= 2:
            raise ValueError("Scope worker count must be 1 or 2")

    def _extend_cooldown(self, until):
        with self._schedule_lock:
            self.cooldown = max(self.cooldown, until)
            self.store.set_meta("source_cooldown_until", self.cooldown)
            return self.cooldown

    def tick(self):
        self.catalog_tick()
        self.scope_tick()

    def catalog_tick(self):
        now = self.clock()
        with self._schedule_lock:
            if now < self.cooldown:
                return
        if now >= self.catalog_due:
            try:
                programs = self.source.list_programs()
                self.store.sync_catalog(programs, self.clock())
                self.store.set_meta("catalog_error", "")
                self.catalog_due = self.clock() + self.settings.catalog_interval
            except SourceError as exc:
                self.store.set_meta("catalog_error", "source_unavailable")
                self.catalog_due = self._extend_cooldown(
                    self.clock() + max(60, exc.retry_after or 0)
                )
                LOG.warning("Catalog refresh failed; retaining last complete catalog")
                return

    def scope_tick(self):
        # Claim before leaving the lock; a second worker must select a different
        # program. Claims are process-local and disappear safely after a restart.
        with self._schedule_lock:
            now = self.clock()
            if now < self.cooldown or len(self._inflight) >= self.settings.scope_workers:
                return
            program = self.store.next_due(now, exclude_ids=tuple(self._inflight))
            if not program:
                return
            self._inflight.add(program["id"])
        try:
            snapshot = self.source.fetch_snapshot(program)
            self.store.apply_snapshot(snapshot, self.clock(), self.settings.scope_refresh)
            self.store.set_meta("scope_error", "")
        except SourceError as exc:
            if getattr(exc, "not_public", False):
                self.store.deactivate_program(program["id"], self.clock())
                self.store.set_meta("scope_error", "")
                return
            delay = max(60, exc.retry_after or 0)
            retry_at = self.store.record_program_failure(
                program["id"], self.clock(), delay, reason="source_unavailable"
            )
            self.store.set_meta("scope_error", "source_unavailable" if retry_at is not None else "")
            if getattr(exc, "status_code", None) in {401, 429} or exc.retry_after:
                # A program's growing retry backoff must not stall unrelated
                # programs or discovery beyond the source/server cooldown.
                self._extend_cooldown(self.clock() + delay)
            LOG.warning("Program snapshot failed; retaining previous complete snapshot")
        except ValueError:
            # Catalog visibility/identity may change while scope pages are fetched.
            # Store validates before writing, so discard this racing snapshot.
            retry_at = self.store.record_program_failure(
                program["id"], self.clock(), 60, reason="snapshot_rejected"
            )
            self.store.set_meta("scope_error", "snapshot_rejected" if retry_at is not None else "")
            LOG.warning("Snapshot rejected; previous state retained")
        finally:
            with self._schedule_lock:
                self._inflight.remove(program["id"])


class Dispatcher:
    def __init__(self, store, telegram, catalog_interval=120, clock=time.time):
        self.store, self.telegram, self.clock = store, telegram, clock
        self.catalog_interval = catalog_interval
        self.cooldown = 0.0
        self.chat_due: dict[str, float] = {}

    def tick(self):
        now = self.clock()
        if now < self.cooldown:
            return
        stats = self.store.status(now)
        fresh = stats.get("last_catalog_at") is not None and now - float(
            stats["last_catalog_at"]
        ) <= max(600, self.catalog_interval * 3)
        for item in self.store.pending_deliveries(now, 100, include_events=fresh):
            if item["event_id"] and not fresh:
                continue
            if now < self.chat_due.get(item["chat_id"], 0):
                continue
            try:
                if item.get("reply_markup") is not None:
                    message_id = self.telegram.send_message(
                        item["chat_id"], item["text"], reply_markup=item["reply_markup"]
                    )
                else:
                    message_id = self.telegram.send_message(item["chat_id"], item["text"])
                self.store.delivered(item["id"], message_id, self.clock())
            except TelegramError as exc:
                self.store.delivery_failed(item["id"], self.clock(), exc.retry_after, exc.permanent)
                self.cooldown = self.clock() + max(1, exc.retry_after or 0)
                LOG.warning("Telegram delivery failed; retry or dead-letter recorded")
            self.chat_due[item["chat_id"]] = self.clock() + 3.1
            if len(self.chat_due) > 10000:
                self.chat_due = {k: v for k, v in self.chat_due.items() if v > self.clock()}
            return


class Service:
    def __init__(self, settings, store, source, telegram):
        self.settings, self.store, self.telegram = settings, store, telegram
        self.monitor = Monitor(store, source, settings)
        self.dispatcher = Dispatcher(store, telegram, settings.catalog_interval)
        self.bot = Bot(store, settings=settings)
        self.stop = threading.Event()
        source.cancel_event = self.stop
        self.failed = threading.Event()
        self._health_due = 0.0

    def health_tick(self, now):
        if not self.settings.admin_chat_id or now < self._health_due:
            return
        self._health_due = now + 60
        if operator_needs_attention(self.store, self.settings, now):
            self.store.queue_text(
                self.settings.admin_chat_id,
                "Tower needs attention: program checks or deliveries are behind. "
                "Send /status for details.",
                f"health:{int(now // 3600)}",
            )

    def command_tick(self):
        offset = int(self.store.get_meta("telegram_offset", "0"))
        try:
            for update in self.telegram.get_updates(offset, timeout=20):
                self.bot.handle(update)
            self.store.set_meta("bot_error", "")
        except TelegramError as exc:
            self.store.set_meta("bot_error", "telegram_unavailable")
            LOG.warning("Telegram command polling failed")
            self.stop.wait(min(60, max(5, exc.retry_after or 0)))

    def _worker(self, name, operation, interval):
        try:
            while not self.stop.is_set():
                self.store.set_meta(f"heartbeat_{name}", str(time.time()))
                operation()
                self.stop.wait(interval)
        except Exception:
            LOG.error("Worker %s stopped unexpectedly; inspect health and restart", name)
            self.store.set_meta("fatal_worker", name)
            self.failed.set()
            self.stop.set()

    def run(self):
        previous_channel = self.store.get_meta("configured_channel", "")
        if previous_channel and previous_channel != self.settings.channel_id:
            self.store.delete_subscription(previous_channel)
        if self.settings.channel_id:
            self.store.set_subscription(self.settings.channel_id, True, {})
        self.store.set_meta("configured_channel", self.settings.channel_id)
        self.store.set_meta("fatal_worker", "")
        operations = [
            ("catalog", self.monitor.catalog_tick, 1),
            ("source", self.monitor.scope_tick, 0.1),
        ]
        if self.settings.scope_workers == 2:
            operations.append(("source_2", self.monitor.scope_tick, 0.1))
        operations.append(("delivery", self.dispatcher.tick, 0.2))
        if self.settings.personal_bot:
            operations.append(("bot", self.command_tick, 0.1))
        threads = [threading.Thread(target=self._worker, args=op, daemon=True) for op in operations]
        for thread in threads:
            thread.start()
        LOG.info("Tower started; public program monitoring enabled")
        last_maintenance = 0.0
        try:
            while not self.stop.wait(1):
                now = time.time()
                self.store.set_meta("heartbeat", str(now))
                if now - last_maintenance >= 3600:
                    self.store.prune(now, self.settings.retention_days)
                    last_maintenance = now
                self.health_tick(now)
        finally:
            self.stop.set()
            deadline = time.monotonic() + 35
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.failed.is_set():
            raise RuntimeError("A worker stopped unexpectedly; see local health status")
