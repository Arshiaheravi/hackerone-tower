# Verification record — 2026-11-04

Environment: Windows, Python 3.11.2, isolated project virtual environment. This consolidates completed checks through November 4; it is not a continuous-uptime guarantee.

For the newer compact-alert rollout on November 5, see the [release record](RELEASE.md). The results below describe the earlier releases.

| Check | Actual result |
| --- | --- |
| Automated suite | **428 tests passed; 95% statement coverage of `tower`** on application revision `f096314` |
| Dependency compatibility | `python -m pip check` passed |
| Ruff lint and formatting | Both checks passed |
| PowerShell parser | Operational scripts passed syntax parsing |
| Credential check | Configured tokens absent from tracked files; `.env` excluded from Git |
| Offline demo | Passed: silent baseline, one fictional scope-change event, mocked Telegram delivery, no duplicate event |
| Live HackerOne preflight | 595 visible public programs; one complete sample with 34 scopes and 18 exclusions |
| Telegram authentication and menu | Authentication succeeded; the 16-command private-chat menu was installed and read back on `f096314`; Telegram accepted the persistent button menu |
| Real Telegram delivery | The user confirmed the welcome message, an earlier labelled fictional scope alert, and the new readable Quick guide; the latest fixture alert was separately accepted by Telegram |
| Schema v3 upgrade | Consistent pre-upgrade backup passed integrity checking; isolated and live migrations preserved every existing row and old column before the operator preference/menu update |
| Backup restoration | `backups/verified-2026-11-04.sqlite3` passed SQLite integrity checking; an isolated restored copy contained 595 programs, 595 snapshots and 25 audit events, with no pending or dead deliveries |
| Windows recovery | Controlled worker termination recovered automatically; a new process and passing health were observed within 57 seconds, with saved snapshots preserved |
| Full live baseline | **595/595 public programs**, containing 47,540 scope records and 372 exclusions; health and readiness both passed |

The live API check exposed a bare-resource program-detail response. The adapter now accepts bare and wrapped details while retaining strict collection, identity, visibility and field validation. Automated tests use isolated fixtures and mocked external delivery; real Telegram checks are separate evidence.

## Guided-menu release: `f096314`

This release adds eight persistent text buttons, readable preferences and monitoring status, a built-in guide, an explicit all-public-program reset, and a concise first-message summary before the complete alert details. The configured operator's alerts are enabled with no program, asset-type or bounty filters. Other subscribers retain their own preferences.

The 428 offline tests cover the actual command-to-SQLite-to-dispatcher-to-mocked-Telegram path, a 429 retry across database restart, duplicate update prevention, all public event kinds, scope/source contracts, private-chat menu validation, migration rollback, and long Unicode alerts. Ruff lint, formatting, dependency compatibility and the offline demo passed. These results measure the tested paths, not perfect reliability or complete behavioral coverage.

Before deployment, the worker was stopped with its recovery task disabled and a consistent backup was saved to `backups/before-menu-v3-20261104.sqlite3`. The backup passed SQLite integrity checking. A copy was migrated first, followed by the live database. Comparisons of every pre-existing column and row across metadata, programs, events, subscriptions, deliveries and SQLite sequence state matched before and after both migrations. Schema v3 adds nullable menu metadata; no existing journal or pending-delivery records were discarded.

The task was then enabled and started with exactly one project launcher and one worker. The persisted upgrade menu was delivered without a failed attempt, and `setup-bot` installed and read back all 16 private-chat commands. The user confirmed that Quick guide appeared in the actual Telegram client. A separate labelled fictional scope alert used a temporary database, survived close/reopen, produced one event and one accepted message in one attempt, and produced no duplicate event when the same snapshot was reapplied. This latest fixture's API acceptance is verified; human receipt of that particular fixture was not separately confirmed.

At 2026-11-04T20:04Z, health and readiness passed with all 595 public programs baselined and no pending or dead deliveries. The catalog and scope worker were advancing. After the maintenance pause, 127 snapshots were less than 15 minutes old and 468 were more than two hours old; the oldest was about 202 minutes old. Completed baseline coverage does not imply every snapshot is current. Refreshes continue within the source request budget; `/status` reports current freshness.

## Earlier live verification

After deployment of application revision `9bf0c2c`, the live monitor detected a public Hilton scope addition at 2026-11-04T16:16:49Z. Telegram accepted all four message parts. The scope's source creation time was 2026-11-03T19:20:10Z, during the monitoring pause; the alert correctly distinguished that source date from the later observation date. This verifies an actual API-to-journal-to-Telegram path, but the user has not separately confirmed reading this particular alert. A subsequent Airtable policy change was also detected, with all 14 message parts accepted. The queue then contained zero pending or dead deliveries.

By 2026-11-04T16:47Z, the live journal contained 12 meaningful changes (11 scope additions and one policy change). Telegram accepted all 48 associated message parts, with no cancelled, pending or dead parts for those events. The earlier 13 image-only entries are separate retained audit history.

## Live findings and corrections

Live monitoring produced **13 image-only events**, retained in audit history. These were false alerts caused by profile-image URL changes, not real scope changes. **141 pending deliveries were cancelled.** The correction ignores image-URL and update-timestamp noise during comparison while preserving those values in snapshots. Revision `1e4ed56` also hides those historical cosmetic events from `/recent` before applying the result limit, while preserving audit and delivery rows. Regression tests cover restart persistence, public/handle filtering, and meaningful changes mixed with cosmetic fields.

Live collection also identified a pagination boundary error in a program with more than 10,000 scopes. HackerOne rejected page 100 at size 100, before the adapter's previous cursor switch. Revision `6f12851` switches before that rejected page. Tests verify every one of 11,000 fixture records across multiple cursor pages, including omitted pagination metadata and short pages. After deployment, the one missing program's retry was prioritized; its complete live snapshot saved **10,942 scopes** and brought coverage to 595/595. The live boundary and documentation distinction are recorded in `docs/SOURCES.md`.

Health checks now cover enabled-worker heartbeats, completed-snapshot progress and pending-delivery age. `baseline_complete` and `ready` distinguish completed coverage from worker health. Baseline collection resumed from **129 of 595 public programs** and completed at **595/595**. All snapshots left from before the pause were refreshed. The oldest completed snapshot was about 28 minutes old at the readiness check, reflecting the time needed to rotate through the catalog and paginated programs. Read `tower status` for current counts and oldest snapshot age; complete baseline coverage does not mean instantaneous updates.

## Operational status and remaining verification

The existing Windows task is **enabled and running**, deployed at application revision `f096314`, with logon and one-minute recovery triggers. Only the verified project worker was terminated for the earlier controlled failure check; no manual restart was issued for that check. A new process, fresh catalog/scope progress, passing health and preserved snapshots were observed within 57 seconds. The recovered launcher remained unchanged with exactly one worker through subsequent scheduled triggers. Later code deployments used deliberate stop/start cycles with the task disabled during maintenance. Power and sleep settings were not changed. An awake, online, logged-in Windows session remains necessary.

Docker build/runtime and hosted GitHub Actions have not been verified; VPS deployment and a sustained production soak remain outstanding. Billing and paid subscriber entitlements are not implemented, and commercial redistribution permission has not been confirmed. The real private-chat delivery check does not verify channel posting permissions. Source polling and Telegram delivery cannot guarantee every transient change, earliest detection, exactly-once delivery or uninterrupted uptime.
