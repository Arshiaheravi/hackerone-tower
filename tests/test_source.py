import base64
import copy
import json
import threading
import traceback
from types import SimpleNamespace

import httpx
import pytest

from tower.domain import diff_snapshots
from tower.source import HackerOneClient, SourceError


class FakeTime:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def clock(self):
        return self.value

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.value += delay


def program(identifier=1, state="public_mode", **extra):
    return {
        "id": str(identifier),
        "type": "program",
        "attributes": {
            "handle": f"example_{identifier}",
            "name": "Example",
            "state": state,
            "offers_bounties": True,
            **extra,
        },
    }


def scope(identifier=1):
    return {
        "id": str(identifier),
        "type": "structured-scope",
        "attributes": {
            "asset_identifier": "example.invalid",
            "asset_type": "URL",
            "eligible_for_bounty": True,
            "eligible_for_submission": True,
        },
    }


def make_source(handler, **kwargs):
    timer = FakeTime()
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    source = HackerOneClient(
        "example_user",
        "fake_token",
        client=client,
        sleep=timer.sleep,
        clock=timer.clock,
        **kwargs,
    )
    return source, timer


def test_public_catalog_is_paginated_and_account_attributes_are_removed():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params.get("page[number]") == "2":
            return httpx.Response(200, json={"data": [program(3)], "links": {}})
        return httpx.Response(
            200,
            json={
                "data": [
                    program(
                        1,
                        number_of_reports_for_user=42,
                        bookmarked=True,
                        bounty_earned_for_user=1234,
                        new_private_attribute="secret",
                    ),
                    program(2, "private_mode"),
                    program(4, None),
                ],
                "links": {"next": "?page[number]=2&page[size]=100"},
            },
        )

    source, _ = make_source(handler)
    result = source.list_programs()
    assert [entry["id"] for entry in result] == ["1", "3"]
    assert result[0] == {
        "id": "1",
        "handle": "example_1",
        "name": "Example",
        "state": "public_mode",
        "offers_bounties": True,
    }
    expected = base64.b64encode(b"example_user:fake_token").decode()
    assert all(request.headers["Authorization"] == "Basic " + expected for request in requests)
    assert all(request.url.host == "api.hackerone.com" for request in requests)


@pytest.mark.parametrize("detail_envelope", ["bare", "wrapped"])
def test_snapshot_collects_all_scopes_and_exclusions_and_rechecks_public_state(detail_envelope):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/structured_scopes"):
            if request.url.params.get("filter[id__gt]") == "1":
                return httpx.Response(200, json={"data": [scope(2)]})
            return httpx.Response(
                200,
                json={
                    "data": [scope(1)],
                    "links": {"next": "?page[number]=2&page[size]=100"},
                },
            )
        if request.url.path.endswith("/scope_exclusions"):
            assert not request.url.query
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "8",
                            "type": "scope-exclusion",
                            "attributes": {"category": "Example", "details": "Published rule"},
                        }
                    ]
                },
            )
        detail = program(number_of_reports_for_user=99, bookmarked=True)
        return httpx.Response(200, json=detail if detail_envelope == "bare" else {"data": detail})

    source, timer = make_source(handler)
    snapshot = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert [item["id"] for item in snapshot["scopes"]] == ["1", "2"]
    assert snapshot["exclusions"] == [
        {"id": "8", "category": "Example", "details": "Published rule"}
    ]
    assert paths[0] == paths[-1] == "/v1/hackers/programs/example_1"
    assert max(timer.sleeps) == pytest.approx(1.5)
    assert "number_of_reports_for_user" not in snapshot["program"]
    assert "bookmarked" not in snapshot["program"]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.invalid/v1/hackers/programs?page[number]=2",
        "http://api.hackerone.com/v1/hackers/programs?page[number]=2",
        "https://api.hackerone.com:444/v1/hackers/programs?page[number]=2",
        "https://user:pass@api.hackerone.com/v1/hackers/programs?page[number]=2",
        "/v1/hackers/me/reports?page[number]=2",
        "?page[number]=2#fragment",
        "?page[number]=2&page[number]=3",
        "?unexpected=3",
        "?page[size]=101",
        "?page[number]=-1",
    ],
)
def test_rejects_unsafe_pagination_without_sending_credentials(link):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [program()], "links": {"next": link}})

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="pagination link"):
        source.list_programs()
    assert len(calls) == 1


def test_does_not_follow_redirect_even_when_injected_client_follows_redirects():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.invalid/"})

    source, _ = make_source(handler)
    with pytest.raises(SourceError) as caught:
        source.list_programs()
    assert caught.value.status_code == 302
    assert len(calls) == 1


def test_rejects_pagination_loop():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [program()],
                "links": {"next": str(request.url)},
            },
        )

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="did not advance"):
        source.list_programs()


def test_rejects_duplicate_resources_across_pages():
    def handler(request):
        links = {} if "page[number]" in request.url.params else {"next": "?page[number]=2"}
        return httpx.Response(200, json={"data": [program()], "links": links})

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="duplicate resources"):
        source.list_programs()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_errors_are_not_retried_or_exposed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fake_token sensitive-response")

    source, _ = make_source(handler)
    with pytest.raises(SourceError) as caught:
        source.list_programs()
    assert caught.value.status_code == status
    assert "fake_token" not in str(caught.value)
    assert "sensitive-response" not in str(caught.value)
    assert len(calls) == 1


def test_network_retries_are_bounded_and_errors_are_redacted():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("fake_token", request=request)

    source, timer = make_source(handler)
    with pytest.raises(SourceError, match="network") as caught:
        source.list_programs()
    assert "fake_token" not in str(caught.value)
    assert caught.value.retry_after == 30.0
    assert len(calls) == 3
    assert timer.sleeps == [1.0, 2.0]


def test_small_retry_after_is_honored_then_request_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"data": []})

    source, timer = make_source(handler)
    assert source.list_programs() == []
    assert len(calls) == 2
    assert timer.sleeps == [3.0]


def test_long_retry_after_returns_cooldown_without_blocking():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    source, timer = make_source(handler)
    with pytest.raises(SourceError) as caught:
        source.list_programs()
    assert caught.value.retry_after == 120
    assert caught.value.status_code == 429
    assert timer.sleeps == []
    with pytest.raises(SourceError, match="cooldown"):
        source.list_programs()
    assert len(calls) == 1


def test_service_retries_succeed_and_every_request_is_paced():
    timestamps = []

    def handler(request):
        timestamps.append(timer.clock())
        if len(timestamps) < 3:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": []})

    source, timer = make_source(handler)
    assert source.list_programs() == []
    assert timestamps == pytest.approx([100, 100.2, 100.4])


def test_private_transition_during_collection_discards_snapshot():
    details = 0

    def handler(request):
        nonlocal details
        if request.url.path.endswith(("/structured_scopes", "/scope_exclusions")):
            return httpx.Response(200, json={"data": []})
        details += 1
        return httpx.Response(
            200, json={"data": program(state="public_mode" if details == 1 else "private_mode")}
        )

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="not confirmed public") as caught:
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert details == 2
    assert caught.value.not_public is True


def test_failed_second_scope_page_never_returns_partial_snapshot():
    def handler(request):
        if request.url.path.endswith("/structured_scopes"):
            if "filter[id__gt]" in request.url.params:
                return httpx.Response(403)
            return httpx.Response(
                200, json={"data": [scope()], "links": {"next": "?page[number]=2"}}
            )
        return httpx.Response(200, json={"data": program()})

    source, _ = make_source(handler)
    with pytest.raises(SourceError) as caught:
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert caught.value.status_code == 403
    assert caught.value.not_public is False


@pytest.mark.parametrize("boundary_link", ["explicit_size", "omitted_size", "omitted_link"])
def test_scope_cursor_reads_beyond_10000_rows_without_offset_pages(
    boundary_link,
):
    scope_requests = []

    def handler(request):
        if request.url.path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        if not request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": program()})
        page = int(request.url.params.get("page[number]", "1"))
        cursor = int(request.url.params.get("filter[id__gt]", "0"))
        scope_requests.append((cursor, page))
        if page >= 100:
            return httpx.Response(400)
        assert page == 1
        start, stop = cursor + 1, min(cursor + 101, 11001)
        batch = [scope(i) for i in range(start, stop)]
        links = (
            {
                "next": "?page[number]=2&page[size]=100"
                + (f"&filter[id__gt]={cursor}" if cursor else "")
            }
            if stop < 11001
            else {}
        )
        if cursor == 9800:
            if boundary_link == "omitted_size":
                links = {"next": "?page[number]=100"}
            elif boundary_link == "omitted_link":
                links = {}
        return httpx.Response(200, json={"data": batch, "links": links})

    source, _ = make_source(handler)
    result = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert [item["id"] for item in result["scopes"]] == [str(i) for i in range(1, 11001)]
    # The exact-full final page gets one empty probe before the snapshot commits.
    assert scope_requests == [(cursor, 1) for cursor in range(0, 11001, 100)]


def test_scope_cursor_follows_short_pages_and_uses_last_observed_nonconsecutive_id():
    scope_requests = []

    def handler(request):
        if request.url.path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        if not request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": program()})
        page = int(request.url.params.get("page[number]", "1"))
        cursor = int(request.url.params.get("filter[id__gt]", "0"))
        scope_requests.append((cursor, page))
        if page >= 100:
            return httpx.Response(400)
        assert page == 1
        if cursor == 0:
            batch = [scope(2), scope(198)]
            links = {"next": "?page[number]=100&page[size]=100"}
        elif cursor == 198:
            batch = [scope(i) for i in range(199, 299)]
            links = {"next": "?filter[id__gt]=198&page[number]=2&page[size]=100"}
        else:
            assert cursor == 298
            batch = [scope(i) for i in range(299, 349)]
            links = {}
        return httpx.Response(200, json={"data": batch, "links": links})

    source, _ = make_source(handler)
    result = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert [item["id"] for item in result["scopes"]] == ["2"] + [str(i) for i in range(198, 349)]
    assert scope_requests == [(0, 1), (198, 1), (298, 1)]
    assert all(page < 100 for _, page in scope_requests)


def test_scope_cursor_prevents_false_removal_when_earlier_rows_change_between_pages():
    rows = list(range(1, 202))
    requests = []

    def handler(request):
        if request.url.path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        if not request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": program()})
        page = int(request.url.params.get("page[number]", "1"))
        cursor = int(request.url.params.get("filter[id__gt]", "0"))
        available = [identifier for identifier in rows if identifier > cursor]
        offset = (page - 1) * 100
        batch = available[offset : offset + 100]
        links = (
            {
                "next": f"?page[number]={page + 1}&page[size]=100"
                + (f"&filter[id__gt]={cursor}" if cursor else "")
            }
            if offset + 100 < len(available)
            else {}
        )
        requests.append((page, cursor))
        if len(requests) == 1:
            rows.remove(1)
            rows.append(202)
        return httpx.Response(200, json={"data": [scope(i) for i in batch], "links": links})

    source, _ = make_source(handler)
    result = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert [item["id"] for item in result["scopes"]] == [str(i) for i in range(1, 203)]
    assert requests == [(1, 0), (1, 100), (1, 200)]
    before = copy.deepcopy(result)
    before["scopes"] = before["scopes"][:-1]
    events = diff_snapshots(before, result, 200, 100)
    assert [(event["kind"], event["after"]["id"]) for event in events] == [("scope_added", "202")]
    # A deletion after an earlier page was read still needs the next full pass;
    # only the actually removed row should then produce a removal alert.
    reconciled = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    events = diff_snapshots(result, reconciled, 300, 100)
    assert [(event["kind"], event["before"]["id"]) for event in events] == [("scope_removed", "1")]


@pytest.mark.parametrize("count, expected_requests", [(0, 1), (99, 1), (100, 2), (101, 2)])
def test_scope_cursor_probes_full_pages_without_next_but_stops_after_short_page(
    count, expected_requests
):
    cursors = []

    def handler(request):
        if request.url.path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        if not request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": program()})
        cursor = int(request.url.params.get("filter[id__gt]", "0"))
        cursors.append(cursor)
        return httpx.Response(
            200,
            json={"data": [scope(i) for i in range(cursor + 1, min(cursor + 101, count + 1))]},
        )

    source, _ = make_source(handler)
    result = source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert len(result["scopes"]) == count
    assert len(cursors) == expected_requests


@pytest.mark.parametrize(
    "next_link",
    [
        "https://unexpected.invalid/v1/hackers/programs/example_1/structured_scopes?page[number]=2",
        "/v1/hackers/programs/example_2/structured_scopes?page[number]=2",
        "?filter[id__gt]=0&page[number]=2",
        "?filter[id__gt]=1&filter[id__gt]=2",
        "?page[number]=2&unexpected=1",
        "?page[size]=100",
        {"href": None},
        2,
    ],
)
def test_scope_cursor_does_not_bypass_invalid_or_nonadvancing_next_link(next_link):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": [scope(1)], "links": {"next": next_link}})
        return httpx.Response(200, json={"data": program()})

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="pagination"):
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert len(calls) == 2
    assert all(request.url.host == "api.hackerone.com" for request in calls)


@pytest.mark.parametrize("second_id", [1, 2])
def test_scope_cursor_rejects_repeated_or_decreasing_ids(second_id):
    def handler(request):
        if not request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": program()})
        if "filter[id__gt]" in request.url.params:
            return httpx.Response(200, json={"data": [scope(second_id)]})
        return httpx.Response(200, json={"data": [scope(2)], "links": {"next": "?page[number]=2"}})

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="duplicate resources|not ordered"):
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})


def test_scope_cursor_rejects_empty_page_with_more_results_link():
    def handler(request):
        if request.url.path.endswith("/structured_scopes"):
            return httpx.Response(200, json={"data": [], "links": {"next": "?page[number]=2"}})
        return httpx.Response(200, json={"data": program()})

    source, _ = make_source(handler)
    with pytest.raises(SourceError, match="did not advance"):
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})


@pytest.mark.parametrize(
    "payload",
    [
        {"data": None},
        {"data": {}},
        {"data": [None]},
        {"data": [program(identifier=0)]},
        {"data": [program()], "links": []},
        {"unexpected": []},
        {"data": [program()], "links": {"next": {}}},
    ],
)
def test_malformed_collections_fail_closed(payload):
    source, _ = make_source(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceError):
        source.list_programs()


def test_malformed_json_is_a_safe_error():
    source, _ = make_source(lambda request: httpx.Response(200, text="fake_token not-json"))
    with pytest.raises(SourceError, match="invalid JSON") as caught:
        source.list_programs()
    assert "fake_token" not in str(caught.value)


def test_invalid_program_handle_is_rejected_before_network():
    source, _ = make_source(lambda request: pytest.fail("unexpected request"))
    with pytest.raises(SourceError, match="handle"):
        source.fetch_snapshot({"id": "1", "handle": "../../me/reports", "state": "public_mode"})


def test_scope_rate_configuration_cannot_exceed_conservative_ceiling():
    with pytest.raises(ValueError, match="40"):
        make_source(lambda request: pytest.fail("unexpected request"), scope_requests_per_minute=50)


def test_scope_retries_share_scope_rate_limit():
    scope_times = []

    def handler(request):
        if request.url.path.endswith("/structured_scopes"):
            scope_times.append(timer.clock())
            if len(scope_times) == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": program()})

    source, timer = make_source(handler)
    source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert scope_times[1] - scope_times[0] == pytest.approx(1.5)


@pytest.mark.parametrize("status", [403, 404])
def test_detail_access_denial_marks_program_not_public(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    source, _ = make_source(handler)
    with pytest.raises(SourceError) as caught:
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert caught.value.not_public is True
    assert caught.value.status_code == status
    assert len(calls) == 1


def test_catalog_auth_failure_does_not_claim_program_visibility_changed():
    source, _ = make_source(lambda request: httpx.Response(403))
    with pytest.raises(SourceError) as caught:
        source.list_programs()
    assert caught.value.not_public is False


def test_cancelled_source_does_not_start_request():
    source, _ = make_source(lambda request: pytest.fail("unexpected request"))
    source.cancel_event = threading.Event()
    source.cancel_event.set()
    with pytest.raises(SourceError, match="stopped"):
        source.list_programs()


def test_cancellation_interrupts_rate_wait(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    source, _ = make_source(handler)
    source.list_programs()
    event = threading.Event()
    source.cancel_event = event

    def cancel_during_wait(delay):
        event.set()
        return True

    monkeypatch.setattr(event, "wait", cancel_during_wait)
    with pytest.raises(SourceError, match="stopped"):
        source.list_programs()
    assert len(calls) == 1


def test_cancellation_stops_retry_backoff():
    event = threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        event.set()
        return httpx.Response(503)

    source, timer = make_source(handler)
    source.cancel_event = event
    with pytest.raises(SourceError, match="stopped"):
        source.list_programs()
    assert len(calls) == 1
    assert timer.sleeps == []


def test_unpaired_unicode_surrogate_is_rejected_before_storage():
    payload = json.dumps({"data": [program(policy="invalid\ud800text")]}, ensure_ascii=True)
    source, _ = make_source(lambda request: httpx.Response(200, content=payload.encode()))
    with pytest.raises(SourceError, match="Unicode"):
        source.list_programs()


def test_deeply_nested_json_is_a_safe_source_failure():
    body = b'{"data":' + b"[" * 2000 + b"]" * 2000 + b"}"
    source, _ = make_source(lambda request: httpx.Response(200, content=body))
    # Parser nesting limits differ across Python versions. If decoding succeeds,
    # collection validation must still reject the non-resource nested arrays.
    with pytest.raises(
        SourceError, match=r"^HackerOne returned (invalid JSON|a malformed resource)$"
    ):
        source.list_programs()


def test_json_parser_recursion_error_is_redacted(monkeypatch):
    def parser_failure(_content):
        raise RecursionError("private-parser-fixture-detail")

    source, _ = make_source(lambda request: httpx.Response(200, content=b'{"data":[]}'))
    monkeypatch.setattr("tower.source.json", SimpleNamespace(loads=parser_failure))
    with pytest.raises(SourceError, match=r"^HackerOne returned invalid JSON$") as error:
        source.list_programs()
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "private-parser-fixture-detail" not in "".join(traceback.format_exception(error.value))


def test_streaming_response_limit_stops_reading_oversized_body():
    class LargeBody(httpx.SyncByteStream):
        def __init__(self):
            self.chunks = 0
            self.closed = False

        def __iter__(self):
            for _ in range(1000):
                self.chunks += 1
                yield b" " * 65536

        def close(self):
            self.closed = True

    body = LargeBody()
    source, _ = make_source(lambda request: httpx.Response(200, stream=body))
    with pytest.raises(SourceError, match="supported size"):
        source.list_programs()
    assert body.chunks == 161
    assert body.closed is True


@pytest.mark.parametrize("payload", [program(), {"id": "1", "type": "program"}, {}])
def test_bare_collection_documents_are_not_accepted_as_complete_catalogs(payload):
    source, _ = make_source(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceError, match="malformed collection"):
        source.list_programs()


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {},
        {"error": "unrelated response"},
        {"id": "1", "type": "report", "attributes": {"state": "public_mode"}},
        {"data": {"id": "1", "type": "scope-exclusion", "attributes": {}}},
    ],
)
def test_detail_documents_must_be_actual_program_resources(payload):
    source, _ = make_source(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceError, match="unexpected resource type") as caught:
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert caught.value.not_public is False


def test_bare_private_program_details_mark_visibility_loss():
    source, _ = make_source(lambda request: httpx.Response(200, json=program(state="private_mode")))
    with pytest.raises(SourceError, match="not confirmed public") as caught:
        source.fetch_snapshot({"id": "1", "handle": "example_1", "state": "public_mode"})
    assert caught.value.not_public is True
