import copy
from urllib.parse import quote

import pytest

from tower.render import render_event


@pytest.fixture
def event():
    return {
        "id": "event-123-not-for-the-alert",
        "kind": "scope_updated",
        "program_id": "7",
        "handle": "example",
        "program": {
            "name": "Example program",
            "state": "public_mode",
            "offers_bounties": True,
            "created_at": "2020-01-01T00:00:00Z",
            "policy": "Full policy must stay in the journal.",
        },
        "observed_at": "2026-11-05T14:00:00Z",
        "first_seen_at": "2026-11-01T00:00:00Z",
        "before": {
            "id": "9",
            "asset_identifier": "api.example.test",
            "asset_type": "URL",
            "eligible_for_bounty": False,
        },
        "after": {
            "id": "9",
            "asset_identifier": "api.example.test",
            "asset_type": "URL",
            "eligible_for_submission": True,
            "eligible_for_bounty": True,
        },
        "changes": {"eligible_for_bounty": {"before": False, "after": True}},
    }


def alert(event):
    parts = render_event(event)
    assert len(parts) == 1
    assert len(parts[0].encode("utf-16-le")) // 2 <= 800
    assert 5 <= len(parts[0].splitlines()) <= 6
    return parts[0]


def test_scope_update_has_only_compact_useful_details(event):
    text = alert(event)
    assert text == (
        "ASSET UPDATED\nProgram: Example program\nAsset: api.example.test\n"
        "Changed: Bounty: No → Yes\n"
        "Detected: 2026-11-05 14:00 UTC\nhttps://hackerone.com/example"
    )
    assert len(text.encode("utf-16-le")) // 2 <= 350
    for omitted in (
        event["id"],
        "Part 1",
        "eligible_for_bounty",
        "2020-",
        "2026-11-01",
        "Full policy",
        '{"',
    ):
        assert omitted not in text


def test_added_asset_has_exactly_the_five_essential_lines(event):
    event["kind"] = "scope_added"
    event["after"]["instruction"] = "Only test the documented API."
    text = alert(event)
    assert text == (
        "NEW ASSET\nProgram: Example program\nAsset: api.example.test\n"
        "Detected: 2026-11-05 14:00 UTC\nhttps://hackerone.com/example"
    )


def test_renamed_asset_identifies_old_and_new_without_instruction_dump(event):
    event["after"]["asset_identifier"] = "new.example.test"
    event["changes"] = {
        "asset_identifier": {"before": "api.example.test", "after": "new.example.test"},
        "instruction": {"before": "OLD INSTRUCTION BODY", "after": "NEW INSTRUCTION BODY"},
    }
    text = alert(event)
    assert "Asset: new.example.test" in text
    assert "Changed: Renamed from api.example.test; Instructions updated" in text
    assert "INSTRUCTION BODY" not in text


def test_removed_asset_uses_before_without_promoting_historical_eligibility(event):
    event["kind"] = "scope_removed"
    event["before"].update(eligible_for_submission=False, eligible_for_bounty=False)
    event["after"]["asset_identifier"] = "wrong-current.example.test"
    text = alert(event)
    assert text.startswith("ASSET REMOVED\n")
    assert "Asset: api.example.test" in text
    assert len(text.splitlines()) == 5
    assert "[out of scope]" not in text
    assert "[no bounty]" not in text
    assert "wrong-current" not in text
    assert "Eligible" not in text
    assert "Bounty:" not in text
    assert "Program bounties:" not in text


@pytest.mark.parametrize(
    "field,marker",
    [("eligible_for_submission", "[out of scope]"), ("eligible_for_bounty", "[no bounty]")],
)
@pytest.mark.parametrize("value", [True, False, None, 1, 0, "true", "false"])
def test_only_explicit_false_eligibility_adds_an_inline_marker(event, field, marker, value):
    event["kind"] = "scope_added"
    event["after"] = {"asset_identifier": "api.example.test", field: value}
    text = alert(event)
    assert (marker in text) is (value is False)
    assert len(text.splitlines()) == 5
    assert "Eligible" not in text


def test_missing_current_asset_never_falls_back_to_old_asset_or_program_bounties(event):
    event["kind"] = "scope_added"
    event["after"] = {"id": "9"}
    text = alert(event)
    assert "Asset: Not provided" in text
    assert "api.example.test" not in text
    assert "Bounty:" not in text


def test_program_discovery_shows_only_scope_count_and_official_link(event):
    event["kind"] = "program_discovered"
    event["after"] = {
        "program": event["program"],
        "scopes": [{"asset_identifier": f"asset-{index}.example.test"} for index in range(5)],
        "exclusions": [{"details": "Full exclusion body should not appear"}],
    }
    event["after"]["scopes"][0]["eligible_for_submission"] = False
    event["after"]["scopes"][1]["eligible_for_bounty"] = False
    text = alert(event)
    assert text == (
        "NEW PUBLIC PROGRAM FOUND\nProgram: Example program\nScope: 5 entries (open link)\n"
        "Detected: 2026-11-05 14:00 UTC\nhttps://hackerone.com/example"
    )
    assert "asset-" not in text
    assert "exclusion body" not in text
    assert "launch" not in text.lower()


@pytest.mark.parametrize("value", [False, True, None, "false", 0])
def test_discovery_marks_only_known_unpaid_programs(event, value):
    event["kind"] = "program_discovered"
    event["program"]["offers_bounties"] = value
    event["after"] = {"scopes": []}
    text = alert(event)
    assert ("Program: Example program [no bounty]" in text) is (value is False)


@pytest.mark.parametrize(
    "snapshot,expected", [({}, "Not available"), ({"scopes": []}, "None listed")]
)
def test_missing_and_empty_discovery_scope_are_distinct(event, snapshot, expected):
    event["kind"] = "program_discovered"
    event["after"] = snapshot
    assert "Scope: " + expected in alert(event)


def test_program_update_uses_friendly_change_summary_not_policy_text(event):
    event["kind"] = "program_updated"
    event["changes"] = {
        "policy": {"before": "OLD LARGE BODY", "after": "NEW LARGE BODY"},
        "offers_bounties": {"before": True, "after": False},
        "submission_state": {"before": "open", "after": "paused"},
    }
    text = alert(event)
    assert text.startswith("PROGRAM UPDATED\n")
    assert "Changed: Bounties: Yes → No; Reports: open → paused; +1 more" in text
    assert "Policy updated" not in text
    assert "BODY" not in text


def test_boolean_change_to_unknown_is_not_false_or_current_eligible(event):
    event["after"]["eligible_for_bounty"] = None
    event["changes"] = {"eligible_for_bounty": {"before": False, "after": None}}
    text = alert(event)
    assert "Bounty: No → Unknown" in text
    assert "Bounty: Eligible" not in text


@pytest.mark.parametrize(
    "kind,title",
    [
        ("exclusion_added", "EXCLUSION ADDED"),
        ("exclusion_updated", "EXCLUSION UPDATED"),
        ("exclusion_removed", "EXCLUSION REMOVED"),
    ],
)
def test_exclusions_name_only_the_current_or_removed_category(event, kind, title):
    event["kind"] = kind
    event["before"] = {"category": "old_category", "details": "Former restriction"}
    event["after"] = {"category": "social_engineering", "details": "Current restriction"}
    event["changes"] = {"details": {"before": "Former restriction", "after": "Current restriction"}}
    text = alert(event)
    assert text.startswith(title + "\n")
    if kind == "exclusion_removed":
        assert "Exclusion: old category" in text
        assert "Current restriction" not in text
    else:
        assert "Exclusion: social engineering" in text
        assert "Former restriction" not in text
    if kind == "exclusion_updated":
        assert "Changed: Restriction updated" in text
    assert "Former restriction" not in text
    assert "Current restriction" not in text


@pytest.mark.parametrize(
    "kind",
    [
        "program_discovered",
        "program_updated",
        "scope_added",
        "scope_updated",
        "scope_removed",
        "exclusion_added",
        "exclusion_updated",
        "exclusion_removed",
        "unknown",
    ],
)
@pytest.mark.parametrize("handle", ["example", "?" * 200])
def test_all_kinds_with_long_unicode_stay_single_bounded_and_explicitly_shortened(
    event, kind, handle
):
    long_text = "\U0001f600漢字e\u0301" * 1000
    event["kind"] = kind
    event["handle"] = handle
    event["program"]["name"] = long_text
    event["program"]["policy"] = "FULL POLICY MARKER" * 1000
    scope = {
        "asset_identifier": long_text,
        "asset_type": long_text,
        "instruction": long_text,
        "category": long_text,
        "details": long_text,
        "eligible_for_submission": False,
        "eligible_for_bounty": False,
    }
    event["before"] = {**scope, "asset_identifier": "OLD" + long_text}
    event["after"] = {"scopes": [scope] * 6} if kind == "program_discovered" else scope
    event["changes"] = {
        key: {"before": long_text, "after": long_text}
        for key in ("asset_type", "max_severity", "submission_state", "policy")
    }
    text = alert(event)
    assert "…" in text
    assert "Details shortened" not in text
    assert text.endswith(
        "Detected: 2026-11-05 14:00 UTC\nhttps://hackerone.com/" + quote(handle, safe="")
    )
    assert "\ufffd" not in text
    assert "FULL POLICY MARKER" not in text
    if kind == "program_discovered":
        assert "Scope: 6 entries (open link)" in text
    if kind in {"scope_added", "scope_updated"}:
        assert " [out of scope] [no bounty]\n" in text


def test_control_characters_and_bidi_are_removed_and_markup_stays_plain(event):
    event["program"]["name"] = "<b>A</b>\r\nFake title\x00\u202e\ud800"
    event["after"]["asset_identifier"] = "[api](https://example.test)\tline\x1b"
    text = alert(event)
    assert "Program: <b>A</b> Fake title" in text
    assert "Asset: [api](https://example.test) line\n" in text
    for control in ("\r", "\t", "\x00", "\u202e", "\ud800", "\x1b"):
        assert control not in text


def test_unrecognized_changed_fields_do_not_echo_arbitrary_names_or_values(event):
    event["changes"] = {
        "SENSITIVE_UNRECOGNIZED_FIELD": {"before": "UNRECOGNIZED_VALUE", "after": "BODY"}
    }
    text = alert(event)
    assert "Other details updated" in text
    assert "SENSITIVE" not in text
    assert "UNRECOGNIZED" not in text
    assert "BODY" not in text


def test_observation_offset_is_converted_to_utc(event):
    event["observed_at"] = "2026-11-05T17:00:00+03:00"
    assert "Detected: 2026-11-05 14:00 UTC" in alert(event)


@pytest.mark.parametrize("value", [None, "unsafe-date-text", "2026-11-05T14:00:00", 123])
def test_missing_invalid_or_naive_observation_does_not_invent_a_date(event, value):
    event["observed_at"] = value
    text = alert(event)
    assert "Detected: Time unavailable" in text
    assert "2020-" not in text
    assert "unsafe-date-text" not in text


@pytest.mark.parametrize("handle", ["../../other?x=1", "a" * 200, "?" * 200])
def test_full_official_destination_is_quoted_and_never_truncated(event, handle):
    event["handle"] = handle
    event["program"]["name"] = "long" * 1000
    event["kind"] = "program_discovered"
    event["after"] = {"scopes": [{"asset_identifier": "\U0001f600" * 1000}] * 7}
    text = alert(event)
    assert text.endswith("https://hackerone.com/" + quote(handle, safe=""))
    assert "Scope: 7 entries (open link)" in text
    assert "…" in text


@pytest.mark.parametrize(
    "handle", [None, "", "a" * 201, "bad\nhandle", "\u202ehandle", "\U0001f600"]
)
def test_invalid_handle_is_rejected_without_echoing_it(event, handle):
    event["handle"] = handle
    with pytest.raises(ValueError, match="^Alert requires a valid bounded program handle$"):
        render_event(event)


def test_missing_name_and_event_id_still_has_program_identity(event):
    event.pop("id")
    event["program"].pop("name")
    assert "Program: example\n" in alert(event)


def test_unknown_event_kind_has_safe_public_fallback(event):
    event["kind"] = "UNRECOGNIZED_KIND"
    text = alert(event)
    assert text.startswith("PUBLIC PROGRAM CHANGE\n")
    assert "Changed: Public details" in text
    assert "UNRECOGNIZED_KIND" not in text


def test_renderer_never_mutates_the_persisted_event(event):
    original = copy.deepcopy(event)
    render_event(event)
    assert event == original


def test_change_summary_keeps_only_two_prioritized_fields_and_remaining_count(event):
    event["changes"] = {
        "policy": {"before": "POLICY BEFORE", "after": "POLICY AFTER"},
        "instruction": {"before": "INSTRUCTIONS BEFORE", "after": "INSTRUCTIONS AFTER"},
        "eligible_for_submission": {"before": True, "after": False},
        "eligible_for_bounty": {"before": True, "after": False},
    }
    event["after"].update(eligible_for_submission=False, eligible_for_bounty=False)
    text = alert(event)
    changed = next(line for line in text.splitlines() if line.startswith("Changed:"))
    assert changed == "Changed: In scope: Yes → No; Bounty: Yes → No; +2 more"
    assert len(changed.encode("utf-16-le")) // 2 <= 150
    assert "Asset: api.example.test [out of scope] [no bounty]" in text
    assert "POLICY" not in text
    assert "INSTRUCTIONS" not in text


def test_long_changed_values_keep_summary_within_150_units(event):
    event["changes"] = {
        field: {"before": "😀" * 300, "after": "😀" * 300}
        for field in ("asset_type", "max_severity", "currency")
    }
    text = alert(event)
    changed = next(line for line in text.splitlines() if line.startswith("Changed:"))
    assert len(changed.encode("utf-16-le")) // 2 <= 150
    assert changed.endswith("; +1 more")
    assert "…" in changed


def test_single_scope_discovery_uses_singular_count(event):
    event["kind"] = "program_discovered"
    event["after"] = {"scopes": [event["after"]]}
    assert "Scope: 1 entry (open link)" in alert(event)
