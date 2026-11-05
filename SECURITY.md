# Security and privacy

HackerOne Tower handles API credentials and Telegram subscription data. Its intended function is to read published public program metadata and deliver notifications. It does not authorize or perform testing against any program asset.

## Reporting a vulnerability in this project

Use this repository's **Security → Report a vulnerability** option if private vulnerability reporting is enabled. If that option is unavailable and no maintainer has supplied a private contact, open a minimal issue asking for a private reporting channel **without vulnerability details or sensitive attachments**.

Include the affected revision, impact, relevant configuration with secrets removed, and minimal steps using local fixtures. Do not test another person's deployment, access private program information, or send messages to real subscribers to demonstrate a report.

Never attach `.env`, authentication headers, token-bearing Telegram URLs, production SQLite files, backups, subscriber IDs or unsanitized logs. Reports about third-party HackerOne programs belong with those programs through their own reporting processes.

Security fixes are developed against the current default branch. There is no published support window, response-time commitment or project bug-bounty program.

## Credential handling

- Keep tokens in a local `.env` or deployment secret store. `.env` is ignored by Git; ignoring a file does not restrict who can read it on the host.
- Restrict access to the runtime account, database, logs and backups. Backups can contain subscription data even after a subscriber deletes current records.
- If a token is exposed, revoke or rotate it with the provider and update the local configuration. Removing the visible text alone does not invalidate it.
- Keep HTTP debug logging disabled: Telegram embeds the bot token in API URLs. Do not paste diagnostic output without checking it for secrets.

## Application boundaries

- The source adapter uses fixed official HTTPS API hosts, rejects redirects, validates pagination destinations, and accepts only confirmed public programs and allowlisted fields.
- Scope assets are metadata. The application never connects to the listed domains, IP addresses, repositories or other targets.
- Source text is rendered as plain text, not executable code or Telegram markup. Reply keyboards are bounded text-only actions with no contact, location or web-app requests.
- Private bot commands validate the chat boundary. Public group/channel commands do not administer personal subscriptions.
- Losing public visibility cancels queued event alerts. Visibility is checked through polling, so source changes are not instantaneous; previously delivered copies cannot be recalled by this design.

These are implemented boundaries backed by tests, not a claim of perfect security. Review the [architecture](docs/ARCHITECTURE.md), [source contracts](docs/SOURCES.md) and [verification record](docs/VERIFICATION.md) for evidence and limitations.

## Subscriber data and deletion

The service stores the Telegram chat ID needed for delivery, subscription preferences and delivery records. It does not store names, Telegram usernames or command history. `/delete_me` removes the subscriber's current subscription and delivery records without retaining a new reply to that deletion command.

Telegram's copies and operator backups follow their own retention. Public event history and completed delivery records are pruned according to configured retention; pending/dead deliveries and current snapshots are retained for continuity. Operators are responsible for access controls, backup retention, privacy/contact information and any deployment-specific obligations.

## Running a public service

Confirm applicable HackerOne redistribution/commercial permission before publishing or selling a feed. Public visibility alone is not a resale license. Verify recovery, restore procedures and real destination permissions, and use independent monitoring for host outages. Billing and paid subscriber authorization are not part of this application.

The [operations guide](docs/OPERATIONS.md) describes backups, migrations, recovery, retention and remaining deployment checks.
