# Contributing

HackerOne Tower is a Python service and Telegram interface for public program metadata. Keep contributions within that boundary: no target scanning, exploit execution, private-program collection or undisclosed scraping fallback.

## Local setup

Use Python 3.11 or newer from a checkout. No live account or `.env` is required for the tests or offline demo.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m tower demo
```

On Linux/macOS, use `python3` to create the environment and `.venv/bin/python` for the remaining commands. Confirm that `python3` is at least version 3.11. On Windows, `scripts/setup.ps1 -Development` is an alternative that also creates `.env` from the template if it is missing; it preserves an existing file.

## Before changing code

Read [AGENTS.md](AGENTS.md) and the relevant [architecture invariants](docs/ARCHITECTURE.md). For lifecycle, persistence or deployment changes, also read [operations](docs/OPERATIONS.md). Keep a change focused and explain the user-visible behavior it fixes or adds.

Use synthetic fixtures and temporary databases. Do not include account payloads, subscriber identifiers, tokens, copied production databases or personal screenshots in commits, logs, issues or pull requests. See [SECURITY.md](SECURITY.md) for sensitive reports.

## Verification

Run these with the project virtual environment's Python:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest --cov=tower --cov-report=term-missing
.\.venv\Scripts\python.exe -m tower demo
```

Use `python -m ruff format .` with the virtual environment activated when formatting is needed. Run focused tests while developing, then the repository checks before submitting. Report unavailable checks honestly; a local pass does not establish hosted CI or live-service success.

| Change area | Useful regression coverage |
| --- | --- |
| Source adapter | Public visibility and field allowlists, authentication origin, pagination/cursors, incomplete or malformed responses, timeouts and cooldowns |
| Change detection | Add/edit/remove identity, before/after evidence, ignored cosmetic changes and filter behavior |
| Store or schema | Explicit migration, rollback, restart persistence, ordering, deduplication and subscriber erasure |
| Telegram and bot | Private-chat boundary, bounded plain-text/keyboard payloads, retry persistence, saved preferences and replay of long command replies or older multipart deliveries |
| Rendering | Five-line new-asset alerts, at most six lines/800 UTF-16 units for any event, explicit false eligibility, detection dates and full official links; complete evidence stays in the journal |
| Operations | Graceful shutdown, instance locking, worker health and script syntax without registering tasks or changing the host |

Tests and demos must remain offline. Mock external HTTP and use temporary storage; never make test execution send Telegram messages, contact scope assets, install tasks, or change power settings. Manual live checks require a deliberately configured destination and are separate from the test suite.

An autouse fixture in [tests/conftest.py](tests/conftest.py) rejects live DNS lookups and socket connections during tests. Do not bypass it to reach a real API. The source demo uses HTTPX MockTransport, and the installed-wheel probe separately blocks network operations.

## Package and container checks

For packaging or release changes, build a wheel and exercise it outside the source checkout:

```powershell
.\.venv\Scripts\python.exe -m pip wheel --disable-pip-version-check --no-deps --wheel-dir dist/review .
.\.venv\Scripts\python.exe -m ci.smoke_package --wheel-dir dist/review
```

Use an output directory containing exactly one Tower wheel. The smoke helper installs that wheel into a temporary target, verifies the import path and registered console entry point, and runs the offline demo from outside the checkout. It uses dependencies already installed in the current Python environment; it is not a dependency resolver test. The probe strips application credential variables and denies live networking. Building can download the pinned build backend.

The [CI workflow](.github/workflows/ci.yml) runs the source demo and wheel checks across Windows/Linux with Python 3.11/3.12. Its separate Docker job builds the image, verifies the unprivileged user, and runs the demo with networking disabled. To reproduce the container runtime check when Docker is available:

```bash
docker build --pull --tag hackerone-tower:ci .
docker run --rm --network none --no-healthcheck --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 --cpus 1 --memory 512m --pids-limit 64 hackerone-tower:ci demo
```

Image building can use the network; the demonstrated container runtime cannot. These checks need no HackerOne or Telegram credentials. Configured CI jobs do not establish that hosted CI or Docker has actually passed; record results and unexecuted checks separately in the [release record](docs/RELEASE.md).

## Documentation and examples

Update the README or operational guide when a behavior or command changes. Preserve the distinction between observed dates and launch dates, between collection completeness and freshness, and between API acceptance and a person reading a message.

[The example alert](docs/assets/README.md) is generated from a fixed fictional fixture using the actual renderer. Regenerate it when a rendering change affects that example; never replace it with real subscriber or account data.

## Submitting a change

Describe the problem, the resulting behavior, and the relevant verification. Include a short fictional before/after example when it helps review. Note migration or rollback considerations for persistent-data changes.

Do not weaken validation or remove a failing assertion to make checks pass. Avoid unrelated dependency upgrades, broad rewrites and generated files that are not needed for the change. The CI configuration is in [.github/workflows/ci.yml](.github/workflows/ci.yml); check actual run results rather than assuming a local pass covers every platform.

Security-sensitive disclosures belong in a private channel as described in [SECURITY.md](SECURITY.md), not a public issue.
