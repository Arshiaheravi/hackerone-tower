"""Read-only, public-only access to the documented HackerOne Hacker API.

This adapter never visits assets listed by programs. Authentication is sent only
to the fixed API origin, and response text is never included in exceptions.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

API_ORIGIN = "https://api.hackerone.com"
PROGRAMS_PATH = "/v1/hackers/programs"
PROGRAM_FIELDS = frozenset(
    {
        "handle",
        "name",
        "currency",
        "policy",
        "submission_state",
        "triage_active",
        "state",
        "started_accepting_at",
        "offers_bounties",
        "open_scope",
        "fast_payments",
        "gold_standard_safe_harbor",
        "allows_bounty_splitting",
        "profile_picture",
    }
)
SCOPE_FIELDS = frozenset(
    {
        "asset_identifier",
        "asset_type",
        "eligible_for_bounty",
        "eligible_for_submission",
        "instruction",
        "max_severity",
        "created_at",
        "updated_at",
        "confidentiality_requirement",
        "integrity_requirement",
        "availability_requirement",
        "reference",
    }
)
EXCLUSION_FIELDS = frozenset({"category", "details", "created_at", "updated_at"})
_HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,99}\Z")
_BOOLEAN_FIELDS = frozenset(
    {
        "triage_active",
        "offers_bounties",
        "open_scope",
        "fast_payments",
        "gold_standard_safe_harbor",
        "allows_bounty_splitting",
        "eligible_for_bounty",
        "eligible_for_submission",
    }
)


class SourceError(Exception):
    """A safe operational error, optionally with a server-directed cooldown."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
        not_public: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code
        self.not_public = not_public


def _resource_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SourceError("HackerOne returned an invalid resource ID")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 32 or int(text) < 1:
        raise SourceError("HackerOne returned an invalid resource ID")
    return str(int(text))


def _flatten(resource: Any, fields: frozenset[str], resource_type: str) -> dict:
    if not isinstance(resource, dict) or resource.get("type") != resource_type:
        raise SourceError("HackerOne returned an unexpected resource type")
    attrs = resource.get("attributes")
    if not isinstance(attrs, dict):
        raise SourceError("HackerOne returned malformed resource attributes")
    result: dict[str, Any] = {"id": _resource_id(resource.get("id"))}
    for key in fields & attrs.keys():
        value = attrs[key]
        if key in _BOOLEAN_FIELDS:
            valid = value is None or isinstance(value, bool)
        else:
            valid = value is None or isinstance(value, str)
        if not valid:
            raise SourceError("HackerOne returned an unexpected attribute value")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise SourceError("HackerOne returned invalid Unicode text") from None
        result[key] = value
    return result


class HackerOneClient:
    """Synchronous API adapter with independently paced scope requests.

    One instance should be shared by the application's source workers so that
    their combined traffic shares the rate limits. Injected clients remain
    owned by the caller; close() closes only clients created by this adapter.
    """

    def __init__(
        self,
        username: str,
        token: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        requests_per_minute: int = 300,
        scope_requests_per_minute: int = 40,
    ) -> None:
        if not username or not token:
            raise ValueError("HackerOne username and API token are required")
        if not 1 <= requests_per_minute <= 600:
            raise ValueError("HackerOne request rate must be between 1 and 600 per minute")
        if not 1 <= scope_requests_per_minute <= 40:
            raise ValueError("Scope request rate must be between 1 and 40 per minute")
        self._owned_client = client is None
        self._client = client or httpx.Client(
            timeout=20.0,
            follow_redirects=False,
            trust_env=False,
        )
        self._auth = httpx.BasicAuth(username, token)
        self._sleep = sleep
        self._clock = clock
        self._interval = 60.0 / requests_per_minute
        self._scope_interval = 60.0 / scope_requests_per_minute
        self._next_request = 0.0
        self._next_scope_request = 0.0
        self._cooldown_until = 0.0
        self._pace_lock = threading.Lock()
        self.cancel_event: threading.Event | None = None

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> HackerOneClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _check_running(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SourceError("Source stopped")

    def _wait(self, delay: float) -> None:
        self._check_running()
        if self.cancel_event is None:
            self._sleep(delay)
        elif self.cancel_event.wait(delay):
            raise SourceError("Source stopped")
        self._check_running()

    def _pace(self, scopes: bool) -> None:
        with self._pace_lock:
            self._check_running()
            now = self._clock()
            cooldown = self._cooldown_until - now
            if cooldown > 0:
                raise SourceError("HackerOne source cooldown is active", retry_after=cooldown)
            ready = max(self._next_request, self._next_scope_request if scopes else 0)
            if ready > now:
                self._wait(min(ready - now, 60.0))
            now = max(self._clock(), ready)
            self._next_request = now + self._interval
            if scopes:
                self._next_scope_request = now + self._scope_interval

    @staticmethod
    def _safe_url(url: str, resource_path: str) -> str:
        try:
            parsed = urlsplit(urljoin(API_ORIGIN + resource_path, url))
            valid = (
                parsed.scheme == "https"
                and parsed.hostname == "api.hackerone.com"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and parsed.path == resource_path
            )
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            allowed = {"page[number]", "page[size]"}
            if resource_path.endswith("/structured_scopes"):
                allowed.add("filter[id__gt]")
            if len({key for key, _ in pairs}) != len(pairs):
                valid = False
            for key, value in pairs:
                if key not in allowed or not value.isascii() or not value.isdigit():
                    valid = False
                    continue
                if len(value) > 32 or int(value) < 1:
                    valid = False
                if key == "page[size]" and int(value) > 100:
                    valid = False
            if not valid:
                raise ValueError
            return urlunsplit(
                ("https", "api.hackerone.com", resource_path, urlencode(sorted(pairs)), "")
            )
        except (ValueError, TypeError, UnicodeError):
            raise SourceError("HackerOne returned an unsafe pagination link") from None

    @staticmethod
    def _retry_delay(response: httpx.Response, default: float) -> float:
        value = response.headers.get("Retry-After")
        if not value:
            return default
        try:
            delay = float(value)
            if not 0 <= delay < float("inf"):
                return default
            return delay
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return default

    def _get(self, url: str, resource_path: str) -> dict:
        url = self._safe_url(url, resource_path)
        for attempt in range(3):
            self._pace(resource_path.endswith("/structured_scopes"))
            try:
                content = bytearray()
                with self._client.stream(
                    "GET",
                    url,
                    auth=self._auth,
                    follow_redirects=False,
                    timeout=20.0,
                    headers={"Accept": "application/json", "User-Agent": "HackerOne-Tower/1.0"},
                ) as response:
                    if response.status_code == 200:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            self._check_running()
                            if len(content) + len(chunk) > 10 * 1024 * 1024:
                                raise SourceError("HackerOne response exceeded the supported size")
                            content.extend(chunk)
            except httpx.RequestError:
                if attempt < 2:
                    self._wait(float(2**attempt))
                    continue
                raise SourceError("HackerOne network request failed", retry_after=30.0) from None
            status = response.status_code
            if status == 429 or 500 <= status <= 599:
                delay = self._retry_delay(response, 60.0 if status == 429 else float(2**attempt))
                with self._pace_lock:
                    self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
                if delay > 5.0 or attempt == 2:
                    raise SourceError(
                        "HackerOne rate limit reached"
                        if status == 429
                        else "HackerOne service unavailable",
                        retry_after=delay,
                        status_code=status,
                    )
                self._wait(delay)
                continue
            if status != 200:
                raise SourceError("HackerOne request rejected", status_code=status)
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeError, RecursionError):
                raise SourceError("HackerOne returned invalid JSON") from None
            if not isinstance(payload, dict):
                raise SourceError("HackerOne returned a malformed API response")
            return payload
        raise AssertionError("unreachable retry state")

    def _collection(self, path: str) -> list[dict]:
        exclusions = path.endswith("/scope_exclusions")
        url = self._safe_url(path if exclusions else path + "?page[size]=100", path)
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        records: list[dict] = []
        scopes = path.endswith("/structured_scopes")
        previous_scope_id = 0
        for _ in range(10000):
            if url in seen_urls:
                raise SourceError("HackerOne pagination did not advance")
            seen_urls.add(url)
            payload = self._get(url, path)
            batch = payload.get("data")
            if not isinstance(batch, list) or len(batch) > (10000 if exclusions else 100):
                raise SourceError("HackerOne returned a malformed collection")
            for resource in batch:
                if not isinstance(resource, dict):
                    raise SourceError("HackerOne returned a malformed resource")
                resource_id = _resource_id(resource.get("id"))
                if resource_id in seen_ids:
                    raise SourceError("HackerOne returned duplicate resources during pagination")
                if scopes and int(resource_id) <= previous_scope_id:
                    raise SourceError("HackerOne scope pagination is not ordered")
                previous_scope_id = int(resource_id)
                seen_ids.add(resource_id)
                records.append(resource)
            links = payload.get("links", {})
            if not isinstance(links, dict):
                raise SourceError("HackerOne returned malformed pagination metadata")
            next_link = links.get("next")
            if isinstance(next_link, dict):
                next_link = next_link.get("href")
                if not isinstance(next_link, str) or not next_link:
                    raise SourceError("HackerOne returned malformed pagination metadata")
            if next_link is not None and not isinstance(next_link, str):
                raise SourceError("HackerOne returned malformed pagination metadata")
            next_url = self._safe_url(next_link, path) if next_link else None
            if next_url and (not batch or next_url in seen_urls):
                raise SourceError("HackerOne pagination did not advance")
            # Scope IDs are ordered by the API. Seek after the last observed ID
            # on every page: offset pages can skip still-present rows when an
            # earlier row is deleted while collection is in progress. Validate
            # returned links above even though scope traversal uses our cursor.
            # Probe beyond a full page even without next, covering capped links
            # and the 10,000-offset boundary without requesting an offset page.
            # This is still an observation, not a transactional source snapshot.
            if scopes:
                current_query = dict(parse_qsl(urlsplit(url).query))
                current_size = int(current_query.get("page[size]", "100"))
                if next_url or len(batch) == current_size:
                    url = self._safe_url(
                        path
                        + "?"
                        + urlencode(
                            {"page[size]": current_size, "filter[id__gt]": previous_scope_id}
                        ),
                        path,
                    )
                    continue
            if next_url:
                url = next_url
            else:
                return records
        raise SourceError("HackerOne pagination exceeded the supported safety limit")

    @staticmethod
    def _public_program(resource: Any) -> dict | None:
        if not isinstance(resource, dict) or not isinstance(resource.get("attributes"), dict):
            raise SourceError("HackerOne returned malformed program attributes")
        if resource["attributes"].get("state") != "public_mode":
            return None
        program = _flatten(resource, PROGRAM_FIELDS, "program")
        if not isinstance(program.get("handle"), str) or not _HANDLE.fullmatch(program["handle"]):
            raise SourceError("HackerOne returned an invalid program handle")
        if not isinstance(program.get("name"), str) or not program["name"].strip():
            raise SourceError("HackerOne returned an invalid program name")
        return program

    def list_programs(self) -> list[dict]:
        programs = []
        handles: set[str] = set()
        for resource in self._collection(PROGRAMS_PATH):
            program = self._public_program(resource)
            if program is not None:
                if program["handle"] in handles:
                    raise SourceError("HackerOne returned duplicate program handles")
                handles.add(program["handle"])
                programs.append(program)
        return programs

    def _program_detail(self, path: str, expected_id: str, handle: str) -> dict:
        try:
            payload = self._get(path, path)
        except SourceError as exc:
            if exc.status_code in (403, 404):
                raise SourceError(
                    "Program is not confirmed public",
                    status_code=exc.status_code,
                    not_public=True,
                ) from None
            raise
        # The live detail endpoint returns a bare program resource, while
        # collection endpoints use a JSON:API data envelope. Accept a wrapped
        # detail too, but never infer a program from an unrelated response.
        resource = payload["data"] if "data" in payload else payload
        if not isinstance(resource, dict) or resource.get("type") != "program":
            raise SourceError("HackerOne returned an unexpected resource type")
        program = self._public_program(resource)
        if program is None:
            raise SourceError("Program is not confirmed public", status_code=403, not_public=True)
        if program["id"] != expected_id or program["handle"] != handle:
            raise SourceError("HackerOne program identity changed during collection")
        return program

    def fetch_snapshot(self, program: dict) -> dict:
        handle = program.get("handle")
        if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
            raise SourceError("Cannot request an invalid program handle")
        expected_id = _resource_id(program.get("id"))
        if program.get("state") != "public_mode":
            raise SourceError("Program is not confirmed public", status_code=403, not_public=True)
        path = PROGRAMS_PATH + "/" + handle
        self._program_detail(path, expected_id, handle)
        scopes = [
            _flatten(item, SCOPE_FIELDS, "structured-scope")
            for item in self._collection(path + "/structured_scopes")
        ]
        for scope in scopes:
            if not isinstance(scope.get("asset_identifier"), str) or not isinstance(
                scope.get("asset_type"), str
            ):
                raise SourceError("HackerOne returned an incomplete scope")
            if not isinstance(scope.get("eligible_for_bounty"), bool) or not isinstance(
                scope.get("eligible_for_submission"), bool
            ):
                raise SourceError("HackerOne returned incomplete scope eligibility")
        exclusions = [
            _flatten(item, EXCLUSION_FIELDS, "scope-exclusion")
            for item in self._collection(path + "/scope_exclusions")
        ]
        # A program can become private while its scope pages are being read.
        # Discard the whole in-memory snapshot if that happened.
        final_program = self._program_detail(path, expected_id, handle)
        return {"program": final_program, "scopes": scopes, "exclusions": exclusions}
