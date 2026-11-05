# Operations

## First live launch

1. Configure `.env` locally. A personal HackerOne token authenticates with its account username. Keep tokens and backups private.
2. Run `python -m tower check-source`, then `python -m tower check`. These are read-only. The first checks one full program snapshot, including exclusions.
3. Run `python -m tower send-test` and confirm the labelled message arrived. `getMe` alone cannot prove channel posting permissions.
4. Start `python -m tower run`, inspect `python -m tower status`, then install the Windows task after baseline progress is verified. The database lock rejects a second worker.
5. Subscribe through `/start`, set a watchlist, pause/resume and check `/status`. Real source changes cannot be manufactured safely; the offline demo tests synthetic changes.

Private-chat onboarding can use `python -m scripts.connect_telegram begin` followed by `complete --wait-seconds 30` after the user presses Start on the generated link. It matches only the one-time setup challenge from a private user chat and does not guess a destination from arbitrary bot visitors. The helper holds the same instance lock as the worker and preserves processed command offsets. Run it before starting the worker. It stores the operator destination in ignored `.env` without printing chat IDs or tokens.

`python -m tower setup-bot` installs and reads back the private-chat command menu, with no messages sent. `python -m scripts.live_delivery_check` sends one explicitly labelled fixture alert to the configured operator chat or channel. It uses a temporary database, verifies a close/reopen before delivery, checks unchanged-snapshot deduplication, and limits retries to three attempts. The result proves API acceptance, not that a person read the message. Network ambiguity can still duplicate this test message.

The project `.venv` already exists in the development workspace. The setup script installs another checkout. Windows execution policy remains controlled by your account/organization; inspect scripts before allowing execution. No script changes power settings or weakens machine execution policy. At-logon operation needs an awake, online, logged-in computer.

## Health and recovery

The Windows installer configures both logon and repeating one-minute recovery triggers. The recovery trigger is scheduled for one minute after registration and omits a repetition duration, so it continues indefinitely; `IgnoreNew` prevents overlapping task instances. This supplies another start opportunity after exits that do not activate Windows' failure-restart setting. It still requires an awake, logged-in user session and does not repair a running but unhealthy worker. Disable the task before stopping it for maintenance, then enable it when ready. For older installations, add only the recovery trigger after verifying the task's owner, executable, arguments and working directory; preserve its actions, settings, principal and unrelated triggers. A configured trigger is not evidence of successful recovery: verify a new process and fresh health after a controlled exit. [Microsoft repetition duration](https://learn.microsoft.com/en-us/windows/win32/taskschd/repetitionpattern-duration), [instance policy](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-multipleinstances).

`status` reports state without live credentials. `health` returns nonzero for a stale heartbeat/catalog, stopped worker, source error or permanent delivery failures. Read safe operational messages in `logs/tower.log`. HTTP logs are disabled because Telegram URLs contain bot tokens; do not enable HTTP debug logging with real credentials.

Health also checks each enabled worker's heartbeat, completed-snapshot progress, and the oldest pending delivery. Delivery/bot heartbeats have a 120-second allowance; catalog and source allowances scale with their configured intervals. Scope progress becomes stale after `max(600, 2 * SCOPE_REFRESH_SECONDS)` and pending delivery age after `max(300, SCOPE_REFRESH_SECONDS)`. These are operational thresholds, not source freshness guarantees.

The freshness target is at least 99% of API-visible public programs checked within `max(1800, 2 * SCOPE_REFRESH_SECONDS)` seconds. Status shows fresh, stale and uncollected counts plus the percentage. Initial missing snapshots get one window before causing a freshness warning; stale existing snapshots cannot be hidden by success on another program. An empty catalog reports no percentage. This measures current snapshot age, not the fraction of all changes detected.

The service checks operational health once a minute and queues at most one operator warning per clock hour, including when freshness falls behind. Telegram warnings are best effort; use independent host monitoring to detect a powered-off computer or a Telegram outage.

Status also reports the number of programs whose last read failed. A failed program keeps health degraded even when the overall freshness target passes. Its error record clears after that program is collected successfully or confirmed inactive. Automatic retries start after 60 seconds, double to a one-hour ceiling, and honor longer server delays. Retries receive bounded priority so a transient failure does not automatically wait behind a complete normal rotation. Program retry deadlines and shared source cooldowns survive restart. An individual program's exponential backoff does not extend the shared cooldown. Do not delete failure state or restart repeatedly to bypass a server cooldown.

`healthy` describes current worker operation. `baseline_complete` records whether every currently active program has a completed snapshot. `ready` requires both. During normal initial collection the service can be healthy while ready is false; this must not be presented as complete scope coverage.

Catalog and scope workers share request limits. `SCOPE_WORKERS=2` is the default; set it to `1` to return to sequential scope collection. Workers reserve different programs, share the existing total request budgets and release reservations even after failure. Reservations require no schema migration and reset after restart; complete snapshots, subscriptions and pending deliveries stay persistent. Command polling and delivery are independent. Failed reads preserve previous complete data. Confirmed nonpublic programs are deactivated and unsent alerts cancelled. Event delivery pauses when catalog freshness exceeds the safety window (at least ten minutes), while commands remain available.

Temporary Telegram failures retry with exponential delay capped at one hour, except longer server `retry_after` values are respected. Permanent failures remain for operator recovery. Correct credentials/permissions then run `python -m tower retry-dead`. A dead legacy multipart event does not block unrelated later updates; its remaining parts wait for recovery. A timeout after Telegram accepted a message can cause a duplicate. Event IDs remain in the local journal for correlation; compact alerts omit technical IDs.

New event notifications use five or six lines. Instruction and restriction text stays in the journal; the official link provides full details. Formatting changes apply when an event is queued: already delivered chat history and previously queued messages keep their original text. Do not delete or rewrite pending deliveries just to change presentation. A source edit affecting many assets still produces one notification per changed asset.

Bot commands have a per-chat burst limit and refill over time to protect the queue. Excess commands are ignored, and `/delete_me` remains available. Command replies take priority over event alerts without reordering existing multipart deliveries. A restart resets the in-memory command budget.

Set `TELEGRAM_ADMIN_CHAT_ID` to your positive private chat ID for periodic source/dead-letter warnings, after opening the bot with `/start`. This is best effort: Telegram cannot warn about its own outage or a powered-off host. Use independent host monitoring for a commercial launch. Docker health marks problems; Compose restarts exited processes, not merely unhealthy containers.

## Data and backups

Schema v4 adds `program_failures` without changing existing snapshot, event, subscription or delivery columns. Migration and schema-version update happen in one transaction. **Before opening the live database with the new release**, disable the Windows recovery task, stop the worker and take a consistent SQLite backup using the prior release or SQLite's backup API. Even `status` opens the store and can migrate it. Verify schema version, integrity and preserved data before restarting. A rehearsal against a private backup can verify the migration without changing production. Earlier schema-v3 code rejects v4; rollback requires the earlier code and a matching pre-migration backup, with all workers stopped. Restoring a backup may replay later sends.

Historical schema v3 added nullable `deliveries.reply_markup_json` for durable text-only Telegram menus. The current release upgrades supported older databases through schema v4, including this column when needed. Migration preserves snapshots, journal rows, subscriptions, update offsets, pending/dead delivery state, multipart ordering and retry times. Use the backup and maintenance procedure above for every upgrade; never copy only the main file of an active WAL database.

The private-chat command menu now has 16 commands. After deployment run `python -m tower setup-bot` to install and read it back. `/menu` sends the persistent buttons through the normal outbox. `/all` changes only that user's preferences; it enables all available public-program alerts and clears all three filters. Source collection continues across all public API programs regardless of personal filters.

- SQLite uses WAL, transactions and an OS process lock. Use local storage, not a network share or synced folder.
- `python -m tower backup backups/tower-2026-11-02.sqlite3` uses consistent SQLite backup and refuses overwrite. Do not copy only the live main database while WAL writes are active.
- Backups contain chat IDs and preferences. Restrict access, encrypt off-machine copies, and choose backup retention. `.gitignore` is not filesystem access control. Protect `.env`, database, logs and backups through account permissions.
- Default 90-day retention applies to completed delivery/event history. Pending/dead records and current/inactive program snapshots remain for continuity. Monitor disk usage.
- `/delete_me` removes the subscription and delivery records without queuing a fresh record containing that chat ID. Telegram's copies and old backups remain subject to their own retention. `/start` creates a new subscription.
- Restoring a backup can replay changes or sends after its observation time. Stop the worker before restoration; retain originals for rollback.

## Linux VPS migration

Docker configuration is provided, but the Windows development machine has no Docker. A Linux build/runtime test remains a separate gate.

1. Verify the revision, build from it, and pin `PYTHON_IMAGE` to a tested official image digest.
2. Stop the Windows task; create a final consistent backup. Securely transfer source and backup. Re-create `.env` locally on the VPS. One bot token must have only one long-polling consumer.
3. Run `docker compose build`, then `docker compose run --rm tower check` and `docker compose run --rm tower check-source`.
4. Initialize the named data volume and restore the backup as `/app/data/tower.sqlite3`, owned by UID/GID `10001:10001`. Do not copy the Windows `.venv`. Preserve data and log volumes.
5. Run `docker compose up -d`; verify `docker compose ps`, `docker compose exec tower python -m tower health`, `/status` and `send-test`. No inbound ports are needed.
6. Keep the prior image, backup and configuration for rollback. Record revision, image digest and actual verification. Do not run both hosts simultaneously.

The container runs unprivileged with a read-only filesystem, dropped capabilities and resource limits; validate those on the VPS. Channel broadcast scales better than individual messages. Measure queue age before expanding the single-process/SQLite design.

## Public or paid launch

Confirm HackerOne's permission for redistribution and commercial use. Public visibility does not establish a resale license. Publish privacy/contact information and realistic freshness expectations. Test actual recovery and backup restoration. Billing, subscriber entitlements and refunds are not implemented.

Potential later additions: opted-in daily digest, saved search presets, source coverage history, and subscription administration. Prioritize measured demand and licensed data; avoid promises of guaranteed earliest detection or bounty success.
