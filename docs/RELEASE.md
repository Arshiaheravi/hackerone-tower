# Release preparation — 2026-11-04

This record covers the public-source release preparation. The earlier Windows deployment and Telegram receipt evidence are recorded separately in [VERIFICATION.md](VERIFICATION.md). A passing test suite does not establish perfect security, uninterrupted operation, or complete coverage of HackerOne.

## Scope pagination and persistent retries: November 5, 2026

Scope collection now advances by the last observed ID on every page. This fixes a reproduced false-removal case: deleting an earlier row shifted offset pagination past a surviving asset. The cursor traversal retains that asset and observes appended IDs once. It still validates returned links and rechecks public visibility. Exact multiples of 100 scopes add an empty-page probe; concurrent edits still require later reconciliation.

Schema v4 stores unresolved failures per program. Success on another program cannot hide a failure, even if overall freshness exceeds 99%. Failures receive bounded priority retries with exponential backoff and persisted deadlines. Global server cooldowns survive restart; one program's growing backoff does not extend the cooldown for unrelated programs. Confirmed deactivation clears failure state and cancels unsent alerts.

**574 offline tests passed with 95% statement coverage** on Windows/Python 3.11.2. New regressions cover deletion during pagination, independent failure visibility, successful recovery, prioritized retries, cooldown isolation, restart persistence, older-schema migrations and transactional rollback. Ruff, formatting, dependency compatibility and the offline demo passed. Independent review found a global-cooldown coupling issue, which was corrected and covered by a regression before this full run. No other blocking findings remained.

A private schema-v3 backup was migrated in isolation. SQLite integrity and foreign-key checks passed; hashes of every existing row in the metadata, programs, events, subscriptions and deliveries tables matched before and after migration. These checks establish specific behavior, not universal change-detection accuracy or sustained uptime. Minimal automatic alerts retain their five/six-line format.

Application revision `83138f7` was then deployed to the existing Windows task. Recovery was disabled during maintenance, the worker stopped, and a new SQLite backup was created at `backups/before-accuracy-83138f7de7ef.sqlite3`. The live v3-to-v4 migration passed the same integrity, foreign-key and complete existing-row comparisons. Configuration was unchanged. The task was re-enabled and one launcher/worker process pair was running afterward.

A read-only live snapshot check collected 102 scope entries across two pages, including a verified ID-cursor request, with a final public-visibility recheck. Both source workers were active, and subsequent catalog and scope collection advanced. At the follow-up observation all 595 tracked public programs were fresh within 30 minutes, the oldest snapshot was about 24.2 minutes old, health/readiness passed, and unresolved program failures plus pending/dead deliveries were zero. This is a point-in-time observation, not a measured fraction of all HackerOne changes detected.

Telegram accepted one explicitly labelled fictional alert in one attempt. The temporary journal passed restart and unchanged-snapshot deduplication checks; human receipt of this particular message was not independently confirmed. GitHub authentication was checked again and remains unavailable, so publishing and hosted CI are still pending.

## Parallel collection and measured freshness: November 5, 2026

Two bounded scope workers now reserve distinct programs while sharing the existing API rate limits. A slow paginated collection can leave the other worker available for new programs and ordinary refreshes. Claims are released on every exit path and reset after restart; complete snapshots, transaction boundaries, privacy validation and pending deliveries remain unchanged. The SQLite schema remains v3; no migration is needed.

Status measures the proportion of API-visible public programs checked within the greater of 30 minutes and twice the scope-refresh interval. The target is 99%; this is snapshot freshness, not change-detection accuracy. Stale or overdue uncollected programs can no longer remain unnoticed merely because other programs are refreshing. Operational warnings are evaluated once a minute and deduplicated to one per clock hour, including across restarts. The five/six-line event format is unchanged.

**528 offline tests passed with 95% statement coverage** on Windows/Python 3.11.2. Real-thread tests cover concurrent claims, large-collection blocking, shared pacing, 429 cooldowns, privacy races, failure cleanup, shutdown and restart deduplication. Freshness tests cover startup grace, an overdue uncollected program, a stale program despite other fresh progress, the 99% count boundary, recovery and operator-warning persistence. Ruff, formatting, dependency compatibility and the offline demo passed. Read-only independent review found no blocking defect.

Official API limits, native notification options, webhook limitations and the reasons not to claim universal detection accuracy are recorded in [SOURCES.md](SOURCES.md#coverage-improvement-research--2026-11-05).

Application revision `d7d88b5` was deployed to the existing Windows task after a consistent backup (`backups/before-coverage-d7d88b565668.sqlite3`, integrity check passed, schema v3). The local catalog wait was reduced from 120 to 60 seconds; credentials and all other environment values were preserved. Both source workers checked in, scope collection advanced, and a subsequent catalog completed about 80.2 seconds after the previous completion (the 60-second wait plus fetch/pacing time). One launcher and one worker process remained running.

At the follow-up observation, all 595 tracked public programs had complete snapshots within the 30-minute window; the oldest was about 25.4 minutes old. Health and readiness passed with no pending/dead deliveries. This point-in-time 100% freshness does not establish 99% detection accuracy or sustained reliability. Telegram accepted one labelled fictional minimal alert in one attempt; temporary-store restart and unchanged-snapshot deduplication passed. Human receipt of this particular message was not independently confirmed. GitHub publishing and hosted CI remain pending account authentication.

## Minimal alerts follow-up: November 5, 2026

The earlier compact format still included too much information. New-asset alerts now contain exactly five lines: event title, program, asset, detection time and official link. Updates add at most one short change summary. New programs show a scope-entry count. Instruction notes, asset-type labels, duplicate handles, restriction paragraphs and shortening boilerplate are omitted. Explicit out-of-scope/no-bounty labels remain where applicable. Every event fits six lines and 800 UTF-16 units; the fictional documentation example is 158 characters.

**484 offline tests passed with 95% statement coverage** on Windows/Python 3.11.2, including 72 renderer cases and the existing privacy, pagination, restart, filtering, audit and delivery checks. Ruff, formatting, dependency compatibility, documentation-fixture consistency and the offline demo passed. No source collection, database schema, dependency or subscription changes are part of this update. Already delivered history and pending messages retain their original text. The complete event journal remains intact.

The deployment and API acceptance statements below describe the earlier compact release, not this follow-up. GitHub authentication, publishing and hosted CI remain pending.

## Compact alerts: November 5, 2026

Application revision `6fa0c50` replaces automatic verbose reports with one compact message per change, capped at 1,400 UTF-16 units. Alerts name the program and asset, show relevant known eligibility or changed fields, and include the detection time and official program link. New-program alerts preview at most three scope entries and point to the remaining entries. Full policies and before/after records remain in the local journal. The guide is shorter, and Recent changes now identifies affected assets.

**465 offline tests passed with 95% statement coverage** in both the previous and patched dependency environments. The checks include 53 renderer cases, retained audit data, legacy multipart retry ordering, and a mixed-format restart. Ruff, formatting, dependency compatibility, documentation consistency and the offline demo passed.

Deployment used a consistent backup at `backups/before-compact-6fa0c50886d4.sqlite3`, which passed SQLite integrity checking and contained all 595 active program snapshots. The Windows recovery task was disabled during the controlled stop, then re-enabled and started. The previously reviewed dependency fixes were installed from local wheels while the worker was stopped; all 17 installed pins match the tested release environment. Configuration was unchanged. No schema migration or queued-message rewrite was performed.

After restart, one launcher and one worker were running, health and readiness passed, all 595 programs remained baselined, and the delivery queue had no pending or dead messages. Polling and host availability still determine freshness; baseline completeness does not imply instantaneous updates.

Telegram accepted one clearly labelled fictional compact alert in one attempt. Its temporary journal survived close/reopen, produced one event and one message, and produced no duplicate event on an unchanged snapshot. This verifies API acceptance, not human receipt of that particular message. Earlier persisted messages retain their original format; newly queued events use the compact formatter.

The local dependency state described in the November 4 preparation notes below is historical: the running Windows monitor now uses the patched pins. GitHub authentication, the first push and hosted CI are still pending.

## Local release gates

The release was checked on Windows with Python 3.11.2 in a separate virtual environment containing the updated dependency pins. The running installation's environment, credentials, database and Windows task were left unchanged.

| Gate | Observed result |
| --- | --- |
| Offline application suite | 428 tests passed; 95% statement coverage of `tower`; DNS and socket connections blocked during tests |
| Dependency compatibility | `pip check` passed with the release pins |
| Lint and formatting | Ruff checks passed |
| Distribution build | `pip wheel --no-deps` built `hackerone_tower-0.1.0-py3-none-any.whl` using the pinned build backend |
| Installed distribution | Wheel installed into temporary storage; its console entry point ran the offline demo from outside the checkout with networking blocked |
| Credential review | Configured sensitive values and encoded forms absent from the reviewed tracked content and reachable Git objects |
| Independent secret scanner | Gitleaks 8.30.1 found no secrets in the existing reachable Git history |
| Dependency advisory metadata | All 17 pinned runtime, development and build packages checked against PyPI version metadata; none of the updated pins had published advisories at the time of the check |
| Live integration read | Release environment authenticated to HackerOne and Telegram and read 595 public programs; no message was sent by this check |

The live integration read validates authentication and TLS connectivity with the updated dependencies. It does not replace scope pagination tests or verify a new channel's posting permissions. Application tests, package smoke tests, and documentation examples use fictional data; real account credentials are never needed by CI.

## Dependency corrections

The release audit found advisories affecting earlier pinned versions. Only affected packages were updated; unrelated package versions and the application stack were retained. Replacement versions were checked for supported Python versions, published wheels, dependency constraints, license compatibility and remaining published advisories before installation.

| Package | Earlier pin | Release pin | Advisory references |
| --- | --- | --- | --- |
| AnyIO | 4.12.1 | 4.14.2 | [TLS hostname handling](https://github.com/advisories/GHSA-82r6-8w77-94w6), [worker stderr handling](https://github.com/advisories/GHSA-5p39-cfhj-2xmp) |
| certifi | 2026.1.4 | 2026.10.19 | [Compromised intermediate CA trust](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) |
| idna | 3.11 | 3.15 | [Input processing limits](https://github.com/advisories/GHSA-65pc-fj4g-8rjx) |
| pytest | 9.0.2 | 9.0.3 | [Published advisory](https://github.com/advisories/GHSA-6w46-j5rx-g56g) |
| Pygments | 2.19.2 | 2.20.0 | [Published advisory](https://github.com/advisories/GHSA-5239-wwwm-4pmq) |
| setuptools | 80.9.0 | 84.1.0 | [Build/install advisory](https://github.com/advisories/GHSA-h35f-9h28-mq5c), [Windows installation paths](https://github.com/advisories/GHSA-xmj3-9gf7-h52w) |

An advisory attached to an installed version is not proof that every affected code path is reachable in Tower. Conversely, an empty advisory list is not proof that no vulnerabilities exist. Dependency metadata changes over time and should be rechecked before later releases.

Changing a requirement file does not update an already-running process. The existing local installation was intentionally kept running during publication work. Apply dependency upgrades to a live installation through the maintenance procedure in [OPERATIONS.md](OPERATIONS.md), then verify health and API connectivity after restart. Do not modify packages underneath a running worker.

## Publication boundary

The public repository is prepared from tracked release files only. Real `.env` files, subscriber databases, backups, runtime logs, local verification records and downloaded tools are excluded. `.env.example` contains empty credential fields and generic configuration only. Documentation assets are labelled fictional fixtures generated by the actual renderer.

Existing local history is preserved. A fresh public snapshot uses the authenticated GitHub account's noreply identity so older personal commit metadata is not published. The exact exported snapshot must pass a final credential and artifact review before its first push.

## Hosted verification

The [GitHub Actions workflow](../.github/workflows/ci.yml) is configured for Python 3.11/3.12 on Windows/Linux, package installation checks, a Docker build and network-disabled runtime demo, and secret scanning. Workflow files alone are not evidence that those jobs ran.

At the time of local preparation, GitHub authentication and the first remote push were still pending. Hosted CI and Docker runtime results must be recorded after their actual runs. No CD job deploys to the existing Windows monitor, no live credentials are stored in Actions, and no container or package is automatically published to a registry. Linux VPS deployment remains a separate operation.
