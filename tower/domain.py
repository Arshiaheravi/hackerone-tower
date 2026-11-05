"""Public program change detection; no HTTP, database, or Telegram dependencies."""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def utc_iso(timestamp: float) -> str:
    if not math.isfinite(timestamp):
        raise ValueError("Timestamp must be finite")
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def validate_program(program: dict[str, Any]) -> None:
    if not isinstance(program, dict):
        raise ValueError("Program must be an object")
    if program.get("state") != "public_mode":
        raise ValueError("Only confirmed public programs may be stored")
    if not isinstance(program.get("id"), str) or not program["id"]:
        raise ValueError("Program requires a nonempty string ID")
    handle = program.get("handle")
    if not isinstance(handle, str) or not handle or len(handle) > 200:
        raise ValueError("Program requires a valid handle")
    if any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in handle
    ):
        raise ValueError("Program handle contains invalid characters")


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {"program", "scopes", "exclusions"}:
        raise ValueError("Snapshot requires program, scopes, and exclusions")
    validate_program(snapshot["program"])
    for collection in ("scopes", "exclusions"):
        items = snapshot[collection]
        if not isinstance(items, list):
            raise ValueError("Snapshot collections must be lists")
        ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("Snapshot items require nonempty string IDs")
            if item["id"] in ids:
                raise ValueError("Duplicate snapshot item ID")
            ids.add(item["id"])


def field_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Ignore timestamp and avatar URL churn while retaining source values in snapshots."""
    return {
        field: {
            "before": copy.deepcopy(before.get(field)),
            "after": copy.deepcopy(after.get(field)),
        }
        for field in sorted(before.keys() | after.keys())
        if field not in {"updated_at", "profile_picture"} and before.get(field) != after.get(field)
    }


def diff_snapshots(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    observed_at: float,
    first_seen_at: float,
) -> list[dict[str, Any]]:
    validate_snapshot(after)
    if before is not None:
        validate_snapshot(before)
        if before["program"]["id"] != after["program"]["id"]:
            raise ValueError("Cannot compare different programs")
    program = after["program"]
    events: list[dict[str, Any]] = []

    def emit(kind: str, old: Any, new: Any, changes: dict[str, Any]) -> None:
        events.append(
            {
                "id": str(uuid4()),
                "kind": kind,
                "program_id": program["id"],
                "handle": program["handle"],
                "program": copy.deepcopy(program),
                "observed_at": utc_iso(observed_at),
                "first_seen_at": utc_iso(first_seen_at),
                "before": copy.deepcopy(old),
                "after": copy.deepcopy(new),
                "changes": changes,
            }
        )

    if before is None:
        emit("program_discovered", None, after, {})
        return events
    changed = field_changes(before["program"], program)
    if changed:
        emit("program_updated", before["program"], program, changed)
    for collection, prefix in (("scopes", "scope"), ("exclusions", "exclusion")):
        old_items = {item["id"]: item for item in before[collection]}
        new_items = {item["id"]: item for item in after[collection]}
        for item_id in sorted(old_items.keys() | new_items.keys()):
            old, new = old_items.get(item_id), new_items.get(item_id)
            if old is None:
                emit(f"{prefix}_added", None, new, {})
            elif new is None:
                emit(f"{prefix}_removed", old, None, {})
            else:
                changed = field_changes(old, new)
                if changed:
                    emit(f"{prefix}_updated", old, new, changed)
    return events


def normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    filters = filters or {}
    if not isinstance(filters, dict) or set(filters) - {"handles", "asset_types", "bounties_only"}:
        raise ValueError("Unknown subscription filter")
    result: dict[str, Any] = {}
    for field in ("handles", "asset_types"):
        values = filters.get(field, [])
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("Filters must be lists of at most 100 values")
        if any(not isinstance(value, str) or not value or len(value) > 200 for value in values):
            raise ValueError("Invalid subscription filter value")
        result[field] = sorted({value.lower() for value in values})
    value = filters.get("bounties_only", False)
    if not isinstance(value, bool):
        raise ValueError("Bounty preference must be boolean")
    result["bounties_only"] = value
    return result


def matches_filters(event: dict[str, Any], filters: dict[str, Any]) -> bool:
    handles = filters.get("handles", [])
    if handles and event["handle"].lower() not in handles:
        return False
    if filters.get("bounties_only") and event["program"].get("offers_bounties") is not True:
        # Subscribers need to hear when a previously bounty-paying program stops.
        if event["kind"] != "program_updated" or not (event.get("before") or {}).get(
            "offers_bounties"
        ):
            return False
    asset_types = filters.get("asset_types", [])
    if not asset_types:
        return True
    if event["kind"].startswith("scope_"):
        return any(
            str((event.get(side) or {}).get("asset_type", "")).lower() in asset_types
            for side in ("before", "after")
        )
    if event["kind"] == "program_discovered":
        return any(
            str(scope.get("asset_type", "")).lower() in asset_types
            for scope in event["after"]["scopes"]
        )
    # Policy and exclusion changes apply across asset types.
    return True
