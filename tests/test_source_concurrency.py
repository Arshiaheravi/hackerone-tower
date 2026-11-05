"""Real-thread source checks with deterministic time and no network access."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tower.source import HackerOneClient, SourceError


class ThreadClock:
    def __init__(self):
        self.value = 100.0
        self.lock = threading.Lock()
        self.local = threading.local()

    def clock(self):
        with self.lock:
            self.local.last_read = self.value
            return self.value

    def sleep(self, delay):
        assert delay >= 0
        with self.lock:
            self.value += delay

    def reservation(self):
        # The final clock read inside _pace belongs to this thread. Another
        # worker advancing fake time before this handler runs cannot alter it.
        return self.local.last_read


def program(number):
    return {
        "id": str(number),
        "type": "program",
        "attributes": {
            "handle": f"example_{number}",
            "name": f"Example {number}",
            "state": "public_mode",
        },
    }


def catalog_program(number):
    resource = program(number)
    return {"id": resource["id"], **resource["attributes"]}


def scope(number):
    return {
        "id": str(number),
        "type": "structured-scope",
        "attributes": {
            "asset_identifier": f"api-{number}.example.test",
            "asset_type": "URL",
            "eligible_for_submission": True,
            "eligible_for_bounty": True,
        },
    }


def source_client(client, timer, **kwargs):
    return HackerOneClient(
        "fixture_user",
        "fixture_token",
        client=client,
        clock=timer.clock,
        sleep=timer.sleep,
        **kwargs,
    )


@pytest.mark.parametrize("global_rate", [60, 300])
def test_two_snapshot_workers_and_catalog_share_one_request_budget(global_rate):
    timer = ThreadClock()
    initial_requests = threading.Barrier(3, timeout=5)
    seen = set()
    records = []
    lock = threading.Lock()

    def handler(request):
        path = request.url.path
        with lock:
            records.append((timer.reservation(), path))
            first = threading.get_ident() not in seen
            seen.add(threading.get_ident())
        if first:
            # All three callers have an HTTP request in flight together.
            initial_requests.wait()
        if path == "/v1/hackers/programs":
            return httpx.Response(200, json={"data": [program(1), program(2)]})
        if path.endswith("/structured_scopes"):
            cursor = int(request.url.params.get("filter[id__gt]", "0"))
            identifier = cursor + 1
            links = (
                {"next": f"?filter[id__gt]={identifier}&page[number]=2"} if identifier < 3 else {}
            )
            return httpx.Response(200, json={"data": [scope(identifier)], "links": links})
        if path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=program(int(path.rsplit("_", 1)[1])))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = source_client(client, timer, requests_per_minute=global_rate)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(source.fetch_snapshot, catalog_program(1))
            second = pool.submit(source.fetch_snapshot, catalog_program(2))
            catalog = pool.submit(source.list_programs)
            snapshots = [first.result(timeout=10), second.result(timeout=10)]
            assert len(catalog.result(timeout=10)) == 2

    assert len(seen) == 3
    assert [item["program"]["id"] for item in snapshots] == ["1", "2"]
    assert all(
        [item["id"] for item in snapshot["scopes"]] == ["1", "2", "3"] for snapshot in snapshots
    )
    assert len(records) == 13
    all_times = sorted(instant for instant, _ in records)
    scope_times = sorted(
        instant for instant, path in records if path.endswith("/structured_scopes")
    )
    assert len(scope_times) == 6
    assert min(b - a for a, b in zip(all_times, all_times[1:])) >= 60 / global_rate - 1e-9
    assert min(b - a for a, b in zip(scope_times, scope_times[1:])) >= 1.5 - 1e-9


def test_scope_429_blocks_other_snapshot_and_catalog_threads_until_shared_cooldown():
    timer = ThreadClock()
    second_started = threading.Event()
    limit_processed = threading.Event()
    calls = []
    limited = False

    def handler(request):
        nonlocal limited
        path = request.url.path
        calls.append(path)
        if path == "/v1/hackers/programs":
            return httpx.Response(200, json={"data": [program(1), program(2)]})
        if path.endswith("example_1"):
            assert second_started.wait(5)
        elif path.endswith("example_2"):
            second_started.set()
            assert limit_processed.wait(5)
        if path.endswith("/structured_scopes"):
            if not limited:
                limited = True
                return httpx.Response(429, headers={"Retry-After": "9"})
            return httpx.Response(200, json={"data": [scope(1)]})
        if path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=program(int(path.rsplit("_", 1)[1])))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = source_client(client, timer)

        def fetch_first():
            try:
                with pytest.raises(SourceError) as caught:
                    source.fetch_snapshot(catalog_program(1))
                return caught.value
            finally:
                limit_processed.set()

        def fetch_second():
            with pytest.raises(SourceError) as caught:
                source.fetch_snapshot(catalog_program(2))
            return caught.value

        def fetch_catalog():
            assert limit_processed.wait(5)
            with pytest.raises(SourceError) as caught:
                source.list_programs()
            return caught.value

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(operation) for operation in (fetch_first, fetch_second, fetch_catalog)
            ]
            errors = [future.result(timeout=10) for future in futures]

        assert errors[0].status_code == 429
        assert all(error.retry_after == pytest.approx(9) for error in errors)
        assert len(calls) == 3
        assert calls.count("/v1/hackers/programs/example_1/structured_scopes") == 1
        assert "/v1/hackers/programs/example_2/structured_scopes" not in calls
        assert "/v1/hackers/programs" not in calls
        timer.sleep(9)
        assert len(source.list_programs()) == 2
        assert source.fetch_snapshot(catalog_program(2))["scopes"][0]["id"] == "1"


def test_concurrent_malicious_pagination_cannot_send_credentials_off_origin():
    timer = ThreadClock()
    initial_requests = threading.Barrier(2, timeout=5)
    calls = []
    seen = set()
    lock = threading.Lock()

    def handler(request):
        with lock:
            calls.append(request)
            first = threading.get_ident() not in seen
            seen.add(threading.get_ident())
        if first:
            initial_requests.wait()
        path = request.url.path
        if path.endswith("/structured_scopes"):
            links = (
                {
                    "next": "https://unexpected.invalid/v1/hackers/programs/example_1/structured_scopes?page[number]=2"
                }
                if "example_1" in path
                else {}
            )
            return httpx.Response(200, json={"data": [scope(1)], "links": links})
        if path.endswith("/scope_exclusions"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=program(int(path.rsplit("_", 1)[1])))

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source = source_client(client, timer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            bad = pool.submit(source.fetch_snapshot, catalog_program(1))
            good = pool.submit(source.fetch_snapshot, catalog_program(2))
            with pytest.raises(SourceError, match="unsafe pagination link"):
                bad.result(timeout=10)
            assert good.result(timeout=10)["program"]["id"] == "2"

    assert len(seen) == 2
    assert len(calls) == 6
    assert all(request.url.host == "api.hackerone.com" for request in calls)
    assert all(request.headers.get("Authorization", "").startswith("Basic ") for request in calls)
