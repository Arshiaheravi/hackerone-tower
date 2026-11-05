from copy import deepcopy

import pytest

from tower.domain import diff_snapshots, matches_filters, normalize_filters, validate_snapshot


def snapshot():
    return {
        "program": {
            "id": "1",
            "handle": "example",
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
        "exclusions": [{"id": "20", "category": "Social engineering", "details": "Excluded"}],
    }


def test_discovery_preserves_snapshot_and_separates_dates():
    value = snapshot()
    events = diff_snapshots(None, value, 200, 100)
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "program_discovered"
    assert event["after"] == value
    assert event["observed_at"] == "1970-01-01T00:03:20Z"
    assert event["first_seen_at"] == "1970-01-01T00:01:40Z"
    value["scopes"].clear()
    assert event["after"]["scopes"]


def test_all_change_kinds_and_verbose_before_after():
    old = snapshot()
    new = deepcopy(old)
    new["program"]["policy"] = "Read this entire policy"
    new["scopes"][0]["eligible_for_bounty"] = False
    new["scopes"].append({"id": "11", "asset_identifier": "new.example.com", "asset_type": "URL"})
    new["exclusions"] = [{"id": "21", "category": "New category", "details": "New exclusion"}]
    events = diff_snapshots(old, new, 200, 100)
    assert {event["kind"] for event in events} == {
        "program_updated",
        "scope_updated",
        "scope_added",
        "exclusion_added",
        "exclusion_removed",
    }
    changed = next(event for event in events if event["kind"] == "scope_updated")
    assert changed["changes"] == {"eligible_for_bounty": {"before": True, "after": False}}
    assert changed["before"]["asset_identifier"] == "example.com"
    assert changed["after"]["eligible_for_bounty"] is False


def test_timestamps_and_collection_order_do_not_create_noise():
    old = snapshot()
    new = deepcopy(old)
    new["program"]["updated_at"] = "2026-11-02"
    new["scopes"][0]["updated_at"] = "2026-11-02"
    new["exclusions"][0]["updated_at"] = "2026-11-02"
    assert diff_snapshots(old, new, 200, 100) == []


@pytest.mark.parametrize("picture", ["https://images.example.test/avatar?signature=renewed", None])
def test_avatar_url_changes_do_not_create_program_events(picture):
    old = snapshot()
    old["program"]["profile_picture"] = "https://images.example.test/avatar?signature=old"
    new = deepcopy(old)
    new["program"]["profile_picture"] = picture
    new["program"]["updated_at"] = "2026-11-02T18:00:00Z"
    assert diff_snapshots(old, new, 200, 100) == []
    assert new["program"]["profile_picture"] == picture


def test_policy_and_scope_changes_still_emit_when_avatar_url_also_changes():
    old = snapshot()
    old["program"]["profile_picture"] = "https://images.example.test/avatar?signature=old"
    new = deepcopy(old)
    new["program"]["profile_picture"] = "https://images.example.test/avatar?signature=renewed"
    new["program"]["policy"] = "Updated testing rules"
    new["scopes"][0]["instruction"] = "Updated scope restrictions"
    events = diff_snapshots(old, new, 200, 100)
    assert [event["kind"] for event in events] == ["program_updated", "scope_updated"]
    assert events[0]["changes"] == {"policy": {"before": None, "after": "Updated testing rules"}}
    assert events[1]["changes"] == {
        "instruction": {"before": None, "after": "Updated scope restrictions"}
    }
    assert all(
        event["program"]["profile_picture"] == new["program"]["profile_picture"] for event in events
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["program"].update(state="private_mode"),
        lambda value: value["program"].update(handle="../secret"),
        lambda value: value["scopes"].append(deepcopy(value["scopes"][0])),
        lambda value: value.pop("exclusions"),
        lambda value: value.update(scopes=None),
    ],
)
def test_rejects_unsafe_or_incomplete_snapshots(mutation):
    value = snapshot()
    mutation(value)
    with pytest.raises(ValueError):
        validate_snapshot(value)


def test_filters_preserve_eligibility_revocation_and_removal_alerts():
    old = snapshot()
    new = deepcopy(old)
    new["scopes"].clear()
    event = diff_snapshots(old, new, 200, 100)[0]
    preferences = normalize_filters(
        {"handles": ["EXAMPLE"], "asset_types": ["URL"], "bounties_only": True}
    )
    assert matches_filters(event, preferences)
    assert not matches_filters(event, normalize_filters({"handles": ["other"]}))
    assert not matches_filters(event, normalize_filters({"asset_types": ["CIDR"]}))
    new = deepcopy(old)
    new["program"]["offers_bounties"] = False
    assert matches_filters(diff_snapshots(old, new, 200, 100)[0], preferences)


def test_program_policy_and_exclusions_match_all_asset_types():
    old = snapshot()
    new = deepcopy(old)
    new["exclusions"][0]["details"] = "Policy changed"
    assert matches_filters(
        diff_snapshots(old, new, 200, 100)[0], normalize_filters({"asset_types": ["CIDR"]})
    )


def test_normalize_filters_rejects_bad_values():
    for filters in (
        {"unknown": True},
        {"handles": "all"},
        {"bounties_only": "yes"},
        {"asset_types": [None]},
    ):
        with pytest.raises(ValueError):
            normalize_filters(filters)
