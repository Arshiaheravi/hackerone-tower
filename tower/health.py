"""Shared operational health and a readable, privacy-safe Telegram status view."""

import math
import time
from datetime import datetime, timezone
from typing import Any

from tower.config import Settings
from tower.store import Store


def _age(timestamp: Any, now: float) -> float | None:
    try:
        value = float(timestamp)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value) or value > now + 120:
        return None
    return max(0.0, now - value)


def health_status(store: Store, settings: Settings, now: float) -> dict[str, Any]:
    result = store.status(now, max(1800, settings.scope_refresh * 2))
    heartbeat_age = _age(store.get_meta("heartbeat"), now)
    catalog_age = _age(result.get("last_catalog_at"), now)
    reasons = []
    if heartbeat_age is None or heartbeat_age > 120:
        reasons.append("heartbeat_missing_or_stale")
    if catalog_age is None or catalog_age > max(600, settings.catalog_interval * 3):
        reasons.append("catalog_missing_or_stale")
    worker_thresholds = {
        "catalog": max(600, settings.catalog_interval * 3),
        "source": max(600, settings.scope_refresh * 2),
        "delivery": 120,
    }
    if settings.personal_bot:
        worker_thresholds["bot"] = 120
    for index in range(2, settings.scope_workers + 1):
        worker_thresholds[f"source_{index}"] = worker_thresholds["source"]
    worker_ages = {
        name: _age(store.get_meta(f"heartbeat_{name}"), now) for name in worker_thresholds
    }
    for name, threshold in worker_thresholds.items():
        if worker_ages[name] is None or worker_ages[name] > threshold:
            reasons.append(f"worker_{name}_missing_or_stale")
    scope_progress_age = _age(result.get("last_scope_at"), now)
    if (
        result["active_programs"] > 0
        and scope_progress_age is not None
        and scope_progress_age > max(600, settings.scope_refresh * 2)
    ):
        reasons.append("scope_progress_stale")
    pending_age = result.get("pending_delivery_age_seconds")
    if pending_age is not None and pending_age > max(300, settings.scope_refresh):
        reasons.append("pending_delivery_stale")
    if store.get_meta("fatal_worker", ""):
        reasons.append("fatal_worker")
    if result["freshness_target_met"] is False and (
        result["stale_programs"] or result["overdue_uncollected_programs"]
    ):
        reasons.append("scope_freshness_target_missed")
    if result["failed_programs"]:
        reasons.append("program_read_failures")
    errors = ["catalog_error", "scope_error"]
    if settings.personal_bot:
        errors.append("bot_error")
    reasons.extend(key for key in errors if store.get_meta(key, ""))
    if result["dead_deliveries"]:
        reasons.append("dead_deliveries")
    result.update(
        {
            "healthy": not reasons,
            "ready": not reasons and result["baseline_complete"],
            "reasons": reasons,
            "heartbeat_age_seconds": heartbeat_age,
            "catalog_age_seconds": catalog_age,
            "scope_progress_age_seconds": scope_progress_age,
            "worker_heartbeat_age_seconds": worker_ages,
        }
    )
    return result


def operator_needs_attention(store: Store, settings: Settings, now: float) -> bool:
    """Warn about degraded monitoring without treating first catalog collection as an outage."""
    status = health_status(store, settings, now)
    reasons = set(status["reasons"])
    if status["last_catalog_at"] is None and not store.get_meta("catalog_error", ""):
        reasons.discard("catalog_missing_or_stale")
    return bool(reasons)


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "Not available yet"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m ago"


def status_text(store: Store, settings: Settings | None = None, now: float | None = None) -> str:
    settings = settings or Settings()
    now = time.time() if now is None else now
    status = health_status(store, settings, now)
    state = (
        "Monitoring active"
        if status["ready"]
        else "Building initial coverage"
        if status["healthy"]
        else "Monitoring needs attention"
    )
    lines = [
        f"HackerOne Tower | {state}",
        f"Checked at: {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M UTC}",
        "",
        "COVERAGE",
        f"Public programs collected: {status['baselined_programs']:,} / {status['active_programs']:,}",
        "Initial collection: "
        + ("Complete" if status["baseline_complete"] else "Still collecting"),
        "",
        "FRESHNESS",
        f"Program catalog checked: {_ago(status['catalog_age_seconds'])}",
        f"Latest complete scope snapshot: {_ago(status['scope_progress_age_seconds'])}",
        f"Oldest program snapshot: {_ago(status['oldest_snapshot_age_seconds'])}",
        "Recent scope coverage: "
        + (
            f"{status['freshness_percent']:.2f}% "
            f"({status['fresh_programs']:,} / {status['active_programs']:,})"
            if status["freshness_percent"] is not None
            else "No public programs available"
        ),
        f"Freshness target: 99% checked within {int(status['freshness_window_seconds'] // 60)} minutes",
        f"Stale snapshots: {status['stale_programs']:,}; not yet collected: {status['uncollected_programs']:,}",
        f"Programs waiting for a successful retry: {status['failed_programs']:,}",
        "",
        "DELIVERY",
        f"Messages waiting: {status['pending_deliveries']:,}",
        f"Messages needing operator attention: {status['dead_deliveries']:,}",
    ]
    if status["pending_delivery_age_seconds"] is not None:
        lines.append(
            f"Oldest waiting message queued: {_ago(status['pending_delivery_age_seconds'])}"
        )
    explanations = {
        "heartbeat_missing_or_stale": "The monitor has not checked in recently.",
        "catalog_missing_or_stale": "The program catalog is not yet current.",
        "scope_progress_stale": "Complete scope snapshots have stopped advancing.",
        "scope_freshness_target_missed": "Some programs have not been refreshed within the freshness target.",
        "pending_delivery_stale": "Some messages have been waiting longer than expected.",
        "fatal_worker": "A background worker stopped; recovery needs checking.",
        "catalog_error": "A catalog refresh failed; previous complete data is retained.",
        "scope_error": "A scope read failed; previous complete data is retained.",
        "program_read_failures": "Some program reads are still failing; automatic retries are scheduled.",
        "bot_error": "Telegram command polling reported a problem.",
        "dead_deliveries": "Some messages could not be delivered and need operator attention.",
    }
    issues = []
    for reason in status["reasons"]:
        message = explanations.get(reason, "A background worker has not checked in recently.")
        if message not in issues:
            issues.append(message)
    if issues:
        lines.extend(["", "NEEDS ATTENTION", *(f"- {issue}" for issue in issues)])
    lines.extend(
        [
            "",
            "Coverage includes public programs returned by the HackerOne API.",
            "Freshness measures recent checks, not the percentage of all changes detected.",
            "Your settings control your alerts, not which public programs the monitor checks.",
            "Scope rotations can take longer than catalog checks; updates are not instantaneous.",
            "Tap My settings for your preferences or Quick guide to learn the alert types.",
        ]
    )
    return "\n".join(lines)
