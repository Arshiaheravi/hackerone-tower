"""Compact plain-text alerts; complete evidence stays in the event journal."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

_LIMIT = 800
_TITLES = {
    "program_discovered": "NEW PUBLIC PROGRAM FOUND",
    "program_updated": "PROGRAM UPDATED",
    "scope_added": "NEW ASSET",
    "scope_updated": "ASSET UPDATED",
    "scope_removed": "ASSET REMOVED",
    "exclusion_added": "EXCLUSION ADDED",
    "exclusion_updated": "EXCLUSION UPDATED",
    "exclusion_removed": "EXCLUSION REMOVED",
}
_CHANGE_LABELS = {
    "eligible_for_submission": "In scope",
    "eligible_for_bounty": "Bounty",
    "asset_identifier": "Asset identifier",
    "offers_bounties": "Bounties",
    "submission_state": "Reports",
    "instruction": "Instructions",
    "policy": "Policy",
    "asset_type": "Asset type",
    "max_severity": "Max severity",
    "category": "Category",
    "details": "Restriction",
    "currency": "Currency",
    "name": "Program name",
    "handle": "Program handle",
    "open_scope": "Open scope",
    "triage_active": "Triage",
    "gold_standard_safe_harbor": "Safe harbor",
    "fast_payments": "Payment terms",
    "allows_bounty_splitting": "Bounty splitting",
    "confidentiality_requirement": "Confidentiality requirement",
    "integrity_requirement": "Integrity requirement",
    "availability_requirement": "Availability requirement",
    "reference": "Scope reference",
}
_BOOLEAN_CHANGES = {
    "eligible_for_submission",
    "eligible_for_bounty",
    "offers_bounties",
    "open_scope",
    "triage_active",
}
_VALUE_CHANGES = _BOOLEAN_CHANGES | {"asset_type", "max_severity", "submission_state", "currency"}


def _line(value: Any, fallback: str = "Not provided") -> str:
    if not isinstance(value, str):
        return fallback
    clean = "".join(
        " " if char.isspace() else char
        for char in value
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(clean.split()) or fallback


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _prefix(value: str, budget: int) -> str:
    used = 0
    for index, char in enumerate(value):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            return value[:index]
    return value


class _CompactText:
    def preview(self, value: Any, budget: int, fallback: str = "Not provided") -> str:
        text = _line(value, fallback)
        if _units(text) <= budget:
            return text
        return _prefix(text, budget - 1).rstrip() + "…"

    def finish(self, title: str, fields: list[tuple[str, str, str]], footer: list[str]) -> str:
        # Preserve labels, restriction markers, dates and the full official destination.
        values = [value for _, value, _ in fields]

        def message() -> str:
            return "\n".join(
                [
                    title,
                    *(
                        prefix + value + suffix
                        for (prefix, _, suffix), value in zip(fields, values)
                    ),
                    *footer,
                ]
            )

        result = message()
        while (excess := _units(result) - _LIMIT) > 0:
            longest = max(range(len(values)), key=lambda index: _units(values[index]))
            values[longest] = self.preview(
                values[longest], max(1, _units(values[longest]) - excess)
            )
            result = message()
        return result


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _handle(event: dict, program: dict) -> str:
    value = event.get("handle") or program.get("handle")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or not value.isascii()
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("Alert requires a valid bounded program handle")
    return value


def _detected(value: Any) -> str:
    if not isinstance(value, str):
        return "Time unavailable"
    try:
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None:
            return "Time unavailable"
        return observed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError):
        return "Time unavailable"


def _changes(value: Any, text: _CompactText) -> tuple[str, str]:
    changes = _record(value)
    summaries = []
    for field, label in _CHANGE_LABELS.items():
        if field not in changes:
            continue
        delta = _record(changes[field])
        if field == "asset_identifier" and delta.get("before"):
            summaries.append("Renamed from " + text.preview(delta["before"], 40))
        elif field in _VALUE_CHANGES:
            values = []
            for side in ("before", "after"):
                item = delta.get(side)
                if field in _BOOLEAN_CHANGES:
                    item = "Yes" if item is True else "No" if item is False else "Unknown"
                values.append(text.preview(item, 20))
            summaries.append(f"{label}: {values[0]} → {values[1]}")
        else:
            summaries.append(f"{label} updated")
    ignored = {"id", "created_at", "updated_at", "profile_picture"}
    if changes.keys() - _CHANGE_LABELS.keys() - ignored:
        summaries.append("Other details updated")
    remaining = f"; +{len(summaries) - 2} more" if len(summaries) > 2 else ""
    summary = text.preview("; ".join(summaries[:2]), 141 - _units(remaining), "Details updated")
    return summary, remaining


def _scope_count(snapshot: dict) -> str:
    scopes = snapshot.get("scopes")
    if not isinstance(scopes, list):
        return "Not available"
    if not scopes:
        return "None listed"
    return f"{len(scopes):,} {'entry' if len(scopes) == 1 else 'entries'} (open link)"


def render_event(event: dict) -> list[str]:
    """Render one five- or six-line alert without modifying the stored event."""
    program = _record(event.get("program"))
    handle = _handle(event, program)
    kind = event.get("kind")
    text = _CompactText()
    bounty_marker = (
        " [no bounty]"
        if kind == "program_discovered" and program.get("offers_bounties") is False
        else ""
    )
    fields = [("Program: ", text.preview(program.get("name"), 80, handle), bounty_marker)]
    before, after = _record(event.get("before")), _record(event.get("after"))
    if kind == "program_discovered":
        fields.append(("Scope: ", _scope_count(after), ""))
    elif kind in {"scope_added", "scope_updated", "scope_removed"}:
        scope = before if kind == "scope_removed" else after
        markers = ""
        if kind != "scope_removed":
            if scope.get("eligible_for_submission") is False:
                markers += " [out of scope]"
            if scope.get("eligible_for_bounty") is False:
                markers += " [no bounty]"
        fields.append(("Asset: ", text.preview(scope.get("asset_identifier"), 220), markers))
        if kind == "scope_updated":
            summary, remaining = _changes(event.get("changes"), text)
            fields.append(("Changed: ", summary, remaining))
    elif kind == "program_updated":
        summary, remaining = _changes(event.get("changes"), text)
        fields.append(("Changed: ", summary, remaining))
    elif kind in {"exclusion_added", "exclusion_updated", "exclusion_removed"}:
        exclusion = before if kind == "exclusion_removed" else after
        category = _line(exclusion.get("category")).replace("_", " ")
        fields.append(("Exclusion: ", text.preview(category, 100), ""))
        if kind == "exclusion_updated":
            summary, remaining = _changes(event.get("changes"), text)
            fields.append(("Changed: ", summary, remaining))
    else:
        fields.append(("Changed: ", "Public details", ""))
    footer = [
        "Detected: " + _detected(event.get("observed_at")),
        f"https://hackerone.com/{quote(handle, safe='')}",
    ]
    return [text.finish(_TITLES.get(kind, "PUBLIC PROGRAM CHANGE"), fields, footer)]
