"""Transactional snapshots, event history, preferences, and Telegram outbox."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from tower.domain import (
    diff_snapshots,
    field_changes,
    matches_filters,
    normalize_filters,
    validate_program,
    validate_snapshot,
)
from tower.keyboards import validate_reply_markup
from tower.render import render_event


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class Store:
    """One connection guarded by a reentrant lock; transactions remain short."""

    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self._db.close()
            raise ValueError("Unsupported database schema version")
        try:
            # executescript otherwise commits an existing transaction. Begin inside
            # the script so table creation, migrations, and the version update
            # all commit together or roll back together.
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS programs(
                    id TEXT PRIMARY KEY, handle TEXT NOT NULL,
                    catalog_json TEXT NOT NULL, snapshot_json TEXT,
                    first_seen_at REAL NOT NULL, last_snapshot_at REAL,
                    active INTEGER NOT NULL, silent INTEGER NOT NULL, due_at REAL NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS programs_due ON programs(active, due_at);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES programs(id),
                    handle TEXT NOT NULL, observed_at REAL NOT NULL, event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_time ON events(observed_at DESC);
                CREATE TABLE IF NOT EXISTS subscriptions(
                    chat_id TEXT PRIMARY KEY, active INTEGER NOT NULL, filters_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT UNIQUE NOT NULL,
                    event_id TEXT REFERENCES events(id), chat_id TEXT NOT NULL, text TEXT NOT NULL,
                    part INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, finished_at REAL, message_id INTEGER,
                    reply_markup_json TEXT
                );
                CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS deliveries_chat ON deliveries(chat_id, id, state);
                CREATE INDEX IF NOT EXISTS deliveries_event_state ON deliveries(event_id, chat_id, state);
                """
            )
            if version < 2 and "priority" not in {
                row[1] for row in self._db.execute("PRAGMA table_info(programs)")
            }:
                self._db.execute(
                    "ALTER TABLE programs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
                )
            if version < 3 and "reply_markup_json" not in {
                row[1] for row in self._db.execute("PRAGMA table_info(deliveries)")
            }:
                self._db.execute("ALTER TABLE deliveries ADD COLUMN reply_markup_json TEXT")
            if version < 4:
                self._db.execute(
                    """CREATE TABLE program_failures(
                        program_id TEXT PRIMARY KEY REFERENCES programs(id) ON DELETE CASCADE,
                        failure_count INTEGER NOT NULL CHECK(failure_count > 0),
                        first_failed_at REAL NOT NULL, last_failed_at REAL NOT NULL,
                        reason TEXT NOT NULL CHECK(reason IN ('source_unavailable','snapshot_rejected')),
                        retry_at REAL NOT NULL
                    )"""
                )
            self._db.execute("PRAGMA user_version=4")
            self._db.commit()
        except Exception:
            self._db.rollback()
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def _set_meta(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._set_meta(key, value)

    def sync_catalog(self, programs: list[dict[str, Any]], now: float) -> dict[str, int]:
        if not isinstance(programs, list) or not math.isfinite(now):
            raise ValueError("Invalid catalog")
        ids, handles = set(), set()
        encoded: list[tuple[dict[str, Any], str]] = []
        for program in programs:
            validate_program(program)
            if program["id"] in ids or program["handle"] in handles:
                raise ValueError("Duplicate catalog program")
            ids.add(program["id"])
            handles.add(program["handle"])
            encoded.append((program, _json(program)))
        with self._lock, self._db:
            initial = self.get_meta("catalog_initialized") is None
            existing = {row["id"]: row for row in self._db.execute("SELECT * FROM programs")}
            retry_floors = {
                row["program_id"]: row["retry_at"]
                for row in self._db.execute("SELECT program_id,retry_at FROM program_failures")
            }
            new_count = 0
            for program, catalog_json in encoded:
                old = existing.get(program["id"])
                if old is None:
                    new_count += 1
                    self._db.execute(
                        "INSERT INTO programs(id,handle,catalog_json,first_seen_at,active,silent,due_at,priority) VALUES(?,?,?,?,1,?,?,?)",
                        (
                            program["id"],
                            program["handle"],
                            catalog_json,
                            now,
                            int(initial),
                            now,
                            0 if initial else 2,
                        ),
                    )
                else:
                    changed = bool(field_changes(json.loads(old["catalog_json"]), program))
                    due = min(old["due_at"], now) if changed or not old["active"] else old["due_at"]
                    if program["id"] in retry_floors:
                        due = max(due, retry_floors[program["id"]])
                    self._db.execute(
                        "UPDATE programs SET handle=?,catalog_json=?,active=1,due_at=?,priority=? WHERE id=?",
                        (
                            program["handle"],
                            catalog_json,
                            due,
                            max(old["priority"], int(changed)),
                            program["id"],
                        ),
                    )
            missing = [program_id for program_id in existing if program_id not in ids]
            self._db.executemany(
                "UPDATE programs SET active=0 WHERE id=?", [(value,) for value in missing]
            )
            self._db.execute(
                "DELETE FROM program_failures WHERE program_id IN (SELECT id FROM programs WHERE active=0)"
            )
            self._db.execute(
                """UPDATE deliveries SET state='cancelled',finished_at=?
                WHERE state IN ('pending','dead') AND event_id IN
                (SELECT e.id FROM events e JOIN programs p ON p.id=e.program_id WHERE p.active=0)""",
                (now,),
            )
            self._set_meta("catalog_initialized", "1")
            self._set_meta("last_catalog_at", now)
            return {"active": len(programs), "new": new_count, "inactive": len(missing)}

    def next_due(self, now: float, *, exclude_ids: tuple[str, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            excluded = (
                " AND id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")" if exclude_ids else ""
            )
            parameters = (now, *exclude_ids)
            # Fresh discoveries jump ahead of baseline work. A bounded burst then
            # gives ordinary refresh work a turn even under continuous changes.
            streak = int(self.get_meta("priority_streak", "0"))
            if streak >= 5:
                row = self._db.execute(
                    "SELECT catalog_json FROM programs WHERE active=1 AND due_at<=? AND priority=0"
                    + excluded
                    + " ORDER BY due_at,first_seen_at,id LIMIT 1",
                    parameters,
                ).fetchone()
                if row:
                    return json.loads(row[0])
            row = self._db.execute(
                "SELECT catalog_json FROM programs WHERE active=1 AND due_at<=?"
                + excluded
                + " ORDER BY priority DESC,due_at,first_seen_at,id LIMIT 1",
                parameters,
            ).fetchone()
            return json.loads(row[0]) if row else None

    def defer_program(self, program_id: str, until: float) -> None:
        if not math.isfinite(until):
            raise ValueError("Invalid retry timestamp")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT priority FROM programs WHERE id=?", (program_id,)
            ).fetchone()
            if row is not None:
                self._record_priority_turn(row["priority"])
            self._db.execute(
                """UPDATE programs SET due_at=MAX(?,COALESCE(
                    (SELECT retry_at FROM program_failures WHERE program_id=programs.id),?)),
                    priority=0 WHERE id=?""",
                (until, until, program_id),
            )

    def record_program_failure(
        self,
        program_id: str,
        now: float,
        retry_after: float = 60,
        reason: str = "source_unavailable",
    ) -> float | None:
        """Persist an unresolved read failure and its bounded, prioritized retry together."""
        if (
            not math.isfinite(now)
            or not math.isfinite(retry_after)
            or retry_after < 0
            or reason not in {"source_unavailable", "snapshot_rejected"}
        ):
            raise ValueError("Invalid program failure")
        with self._lock, self._db:
            program = self._db.execute(
                "SELECT active,priority FROM programs WHERE id=?", (program_id,)
            ).fetchone()
            if program is None or not program["active"]:
                return None
            previous = self._db.execute(
                "SELECT * FROM program_failures WHERE program_id=?", (program_id,)
            ).fetchone()
            count = previous["failure_count"] + 1 if previous else 1
            delay = max(min(3600, 60 * 2 ** min(count - 1, 6)), retry_after)
            retry_at = max(now + delay, previous["retry_at"] if previous else now)
            if not math.isfinite(retry_at):
                raise ValueError("Invalid program retry timestamp")
            self._db.execute(
                """INSERT INTO program_failures(program_id,failure_count,first_failed_at,
                    last_failed_at,reason,retry_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(program_id) DO UPDATE SET failure_count=excluded.failure_count,
                    last_failed_at=excluded.last_failed_at,reason=excluded.reason,
                    retry_at=excluded.retry_at""",
                (program_id, count, now, now, reason, retry_at),
            )
            self._db.execute(
                "UPDATE programs SET due_at=?,priority=1 WHERE id=?", (retry_at, program_id)
            )
            self._record_priority_turn(program["priority"])
            return retry_at

    def _record_priority_turn(self, priority: int) -> None:
        self._set_meta(
            "priority_streak",
            min(5, int(self.get_meta("priority_streak", "0")) + 1) if priority else 0,
        )

    def deactivate_program(self, program_id: str, now: float) -> None:
        """Suppress publication immediately when a detail response loses public status."""
        with self._lock, self._db:
            self._db.execute("UPDATE programs SET active=0 WHERE id=?", (program_id,))
            self._db.execute("DELETE FROM program_failures WHERE program_id=?", (program_id,))
            self._db.execute(
                """UPDATE deliveries SET state='cancelled',finished_at=?
                WHERE state IN ('pending','dead') AND event_id IN
                (SELECT id FROM events WHERE program_id=?)""",
                (now, program_id),
            )

    def apply_snapshot(
        self, snapshot: dict[str, Any], now: float, refresh_seconds: float
    ) -> list[dict[str, Any]]:
        validate_snapshot(snapshot)
        if not math.isfinite(now) or not math.isfinite(refresh_seconds) or refresh_seconds < 0:
            raise ValueError("Invalid snapshot schedule")
        snapshot_json = _json(snapshot)
        program = snapshot["program"]
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM programs WHERE id=?", (program["id"],)).fetchone()
            if row is None or not row["active"] or row["handle"] != program["handle"]:
                raise ValueError("Snapshot does not match an active catalog program")
            old = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
            events = (
                []
                if old is None and row["silent"]
                else diff_snapshots(old, snapshot, now, row["first_seen_at"])
            )
            subscriptions = self._db.execute(
                "SELECT * FROM subscriptions WHERE active=1"
            ).fetchall()
            for event in events:
                self._db.execute(
                    "INSERT INTO events(id,program_id,handle,observed_at,event_json) VALUES(?,?,?,?,?)",
                    (event["id"], program["id"], program["handle"], now, _json(event)),
                )
                texts = render_event(event)
                for subscriber in subscriptions:
                    if not matches_filters(event, json.loads(subscriber["filters_json"])):
                        continue
                    for part, text in enumerate(texts, 1):
                        self._db.execute(
                            "INSERT INTO deliveries(dedup_key,event_id,chat_id,text,part,created_at) VALUES(?,?,?,?,?,?)",
                            (
                                f"event:{event['id']}:{subscriber['chat_id']}:{part}",
                                event["id"],
                                subscriber["chat_id"],
                                text,
                                part,
                                now,
                            ),
                        )
            self._db.execute(
                "UPDATE programs SET snapshot_json=?,last_snapshot_at=?,silent=0,due_at=?,priority=0 WHERE id=?",
                (snapshot_json, now, now + refresh_seconds, program["id"]),
            )
            self._db.execute("DELETE FROM program_failures WHERE program_id=?", (program["id"],))
            self._set_meta("last_scope_at", now)
            self._record_priority_turn(row["priority"])
            return events

    @staticmethod
    def _chat_id(chat_id: str) -> str:
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 200:
            raise ValueError("Invalid chat ID")
        return chat_id

    def set_subscription(
        self, chat_id: str, active: bool = True, filters: dict[str, Any] | None = None
    ) -> None:
        self._chat_id(chat_id)
        with self._lock, self._db:
            current = self.get_subscription(chat_id)
            preferences = normalize_filters(
                filters if filters is not None else (current or {}).get("filters")
            )
            self._db.execute(
                """INSERT INTO subscriptions(chat_id,active,filters_json) VALUES(?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET active=excluded.active,filters_json=excluded.filters_json""",
                (chat_id, int(active), _json(preferences)),
            )
            # Preference changes and pauses cancel already queued alerts that no longer match.
            for row in self._db.execute(
                "SELECT d.id,e.event_json FROM deliveries d JOIN events e ON e.id=d.event_id WHERE d.chat_id=? AND d.state IN ('pending','dead')",
                (chat_id,),
            ).fetchall():
                if not active or not matches_filters(json.loads(row["event_json"]), preferences):
                    self._db.execute(
                        "UPDATE deliveries SET state='cancelled',finished_at=? WHERE id=?",
                        (time.time(), row["id"]),
                    )

    def get_subscription(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM subscriptions WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return (
                {
                    "chat_id": row["chat_id"],
                    "active": bool(row["active"]),
                    "filters": json.loads(row["filters_json"]),
                }
                if row
                else None
            )

    def delete_subscription(self, chat_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM deliveries WHERE chat_id=?", (chat_id,))
            self._db.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))

    def queue_text(
        self, chat_id: str, text: str, key: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        self._chat_id(chat_id)
        if not isinstance(text, str) or not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Message must contain 1 to 4096 UTF-16 units")
        markup = validate_reply_markup(reply_markup)
        if markup is not None and (
            not chat_id.isascii() or not chat_id.isdecimal() or chat_id.startswith("0")
        ):
            raise ValueError("Reply keyboards require a canonical positive private chat ID")
        markup_json = _json(markup) if markup is not None else None
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO deliveries(dedup_key,chat_id,text,created_at,reply_markup_json) VALUES(?,?,?,?,?)",
                (f"reply:{chat_id}:{key}", chat_id, text, time.time(), markup_json),
            )

    def pending_deliveries(
        self, now: float, limit: int = 10, include_events: bool = True
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """WITH blocked_events AS (
                    SELECT event_id,chat_id FROM deliveries
                    WHERE state='dead' AND event_id IS NOT NULL
                    GROUP BY event_id,chat_id
                )
                SELECT d.* FROM deliveries d
                WHERE d.state='pending' AND d.next_attempt_at<=?
                AND (? OR d.event_id IS NULL)
                AND (d.event_id IS NULL OR (EXISTS(
                    SELECT 1 FROM events e JOIN programs p ON p.id=e.program_id
                    JOIN subscriptions s ON s.chat_id=d.chat_id
                    WHERE e.id=d.event_id AND p.active=1 AND s.active=1)
                    AND NOT EXISTS(SELECT 1 FROM blocked_events blocked
                        WHERE blocked.event_id=d.event_id AND blocked.chat_id=d.chat_id)))
                AND NOT EXISTS(SELECT 1 FROM deliveries prior
                    WHERE prior.chat_id=d.chat_id AND prior.id<d.id AND prior.state='pending'
                    AND ((d.event_id IS NULL AND prior.event_id IS NULL)
                        OR (d.event_id IS NOT NULL AND prior.event_id IS NOT NULL
                            AND NOT EXISTS(SELECT 1 FROM blocked_events blocked
                                WHERE blocked.event_id=prior.event_id
                                AND blocked.chat_id=prior.chat_id))))
                ORDER BY (d.event_id IS NOT NULL),d.id LIMIT ?""",
                (now, int(include_events), max(1, min(limit, 1000))),
            ).fetchall()
            result = []
            for row in rows:
                delivery = dict(row)
                markup_json = delivery.pop("reply_markup_json")
                delivery["reply_markup"] = (
                    json.loads(markup_json) if markup_json is not None else None
                )
                result.append(delivery)
            return result

    def delivered(self, delivery_id: int, message_id: int, now: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE deliveries SET state='delivered',finished_at=?,message_id=? WHERE id=? AND state='pending'",
                (now, message_id, delivery_id),
            )

    def delivery_failed(
        self,
        delivery_id: int,
        now: float,
        retry_after: float | None = None,
        permanent: bool = False,
    ) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT attempts FROM deliveries WHERE id=? AND state='pending'", (delivery_id,)
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            delay = min(3600.0, 2.0 ** min(attempts, 12))
            if retry_after is not None and math.isfinite(retry_after):
                delay = max(delay, max(0.0, retry_after))
            state = "dead" if permanent else "pending"
            self._db.execute(
                "UPDATE deliveries SET attempts=?,state=?,next_attempt_at=?,finished_at=? WHERE id=?",
                (attempts, state, now + delay, now if permanent else None, delivery_id),
            )

    def retry_dead(self) -> int:
        with self._lock, self._db:
            result = self._db.execute(
                """UPDATE deliveries SET state='pending',next_attempt_at=0,finished_at=NULL
                WHERE state='dead' AND (event_id IS NULL OR EXISTS(
                    SELECT 1 FROM events e JOIN programs p ON p.id=e.program_id
                    JOIN subscriptions s ON s.chat_id=deliveries.chat_id
                    WHERE e.id=deliveries.event_id AND p.active=1 AND s.active=1))"""
            )
            return result.rowcount

    def status(self, now: float, freshness_seconds: float = 1800) -> dict[str, Any]:
        if not math.isfinite(now) or not math.isfinite(freshness_seconds) or freshness_seconds <= 0:
            raise ValueError("Invalid freshness window")
        with self._lock:
            counts = self._db.execute(
                """SELECT COUNT(*) AS active_programs,
                COUNT(snapshot_json) AS baselined_programs,
                MIN(last_snapshot_at) AS oldest_snapshot_at,
                COUNT(CASE WHEN snapshot_json IS NOT NULL
                    AND last_snapshot_at BETWEEN ? AND ? THEN 1 END) AS fresh_programs,
                COUNT(CASE WHEN snapshot_json IS NULL AND first_seen_at < ?
                    THEN 1 END) AS overdue_uncollected_programs
                FROM programs WHERE active=1""",
                (now - freshness_seconds, now + 120, now - freshness_seconds),
            ).fetchone()
            result = dict(counts)
            failures = self._db.execute(
                """SELECT COUNT(*) AS failed_programs,MIN(f.first_failed_at) AS oldest_failure_at
                FROM program_failures f JOIN programs p ON p.id=f.program_id WHERE p.active=1"""
            ).fetchone()
            result["failed_programs"] = failures["failed_programs"]
            result["oldest_program_failure_age_seconds"] = (
                max(0.0, now - failures["oldest_failure_at"])
                if failures["oldest_failure_at"] is not None
                else None
            )
            result["freshness_window_seconds"] = freshness_seconds
            result["stale_programs"] = counts["baselined_programs"] - counts["fresh_programs"]
            result["uncollected_programs"] = (
                counts["active_programs"] - counts["baselined_programs"]
            )
            result["freshness_percent"] = (
                round(100 * counts["fresh_programs"] / counts["active_programs"], 2)
                if counts["active_programs"]
                else None
            )
            # Compare counts, not a rounded percentage, at the target boundary.
            result["freshness_target_met"] = (
                counts["fresh_programs"] * 100 >= counts["active_programs"] * 99
                if counts["active_programs"]
                else None
            )
            result["baseline_complete"] = (
                self.get_meta("catalog_initialized") == "1"
                and counts["baselined_programs"] == counts["active_programs"]
            )
            result["oldest_snapshot_age_seconds"] = (
                max(0.0, now - counts["oldest_snapshot_at"])
                if counts["oldest_snapshot_at"] is not None
                else None
            )
            for key in ("last_catalog_at", "last_scope_at"):
                value = self.get_meta(key)
                result[key] = float(value) if value is not None else None
            for state in ("pending", "dead"):
                result[f"{state}_deliveries"] = self._db.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE state=?", (state,)
                ).fetchone()[0]
            oldest_pending = self._db.execute(
                "SELECT MIN(created_at) FROM deliveries WHERE state='pending'"
            ).fetchone()[0]
            result["pending_delivery_age_seconds"] = (
                max(0.0, now - oldest_pending) if oldest_pending is not None else None
            )
            result["subscriptions"] = self._db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE active=1"
            ).fetchone()[0]
            result["event_count"] = self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            return result

    def recent_events(self, limit: int = 5, handle: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT e.event_json FROM events e JOIN programs p ON p.id=e.program_id
                WHERE p.active=1 AND (? IS NULL OR e.handle=?)
                ORDER BY e.observed_at DESC,e.rowid DESC""",
                (handle, handle),
            )
            result = []
            for row in rows:
                event = json.loads(row[0])
                changes = event.get("changes")
                # Keep historical cosmetic noise in the audit journal, but do
                # not let it crowd meaningful changes out of the public feed.
                if (
                    event.get("kind") == "program_updated"
                    and isinstance(changes, dict)
                    and changes
                    and set(changes) <= {"profile_picture", "updated_at"}
                ):
                    continue
                result.append(event)
                if len(result) >= max(1, min(limit, 100)):
                    break
            return result

    def get_program(self, handle: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT snapshot_json FROM programs WHERE active=1 AND handle=?", (handle,)
            ).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def prune(self, now: float, retention_days: int = 90) -> None:
        if retention_days < 1:
            raise ValueError("Retention must be at least one day")
        cutoff = now - retention_days * 86400
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM deliveries WHERE state IN ('delivered','cancelled') AND finished_at<?",
                (cutoff,),
            )
            self._db.execute(
                "DELETE FROM events WHERE observed_at<? AND NOT EXISTS(SELECT 1 FROM deliveries WHERE deliveries.event_id=events.id)",
                (cutoff,),
            )

    def backup(self, path: str | Path) -> None:
        destination = Path(path)
        if self.path is not None and destination.resolve() == self.path.resolve():
            raise ValueError("Backup destination must differ from active database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(destination) as target:
            self._db.backup(target)
