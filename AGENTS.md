# Repository guidance

Python 3.11+ service, httpx adapters, SQLite, Telegram Bot API. Preserve the public-metadata-only boundary; never contact scope assets. Never print `.env`, auth headers, bot-token URLs, private payloads or subscriber IDs in logs.

Use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on Linux. Checks: `-m pip check`, `-m ruff check .`, `-m ruff format --check .`, `-m pytest --cov=tower --cov-report=term-missing`, `-m tower demo`. Tests must be offline. External delivery needs configured credentials and a designated destination. Do not register tasks, change power settings, push, deploy or bill users as test side effects.

Source changes require privacy/pagination/contract tests. Store changes require transaction/restart tests and explicit schema migrations. Preserve pending deliveries and subscriber privacy; separate source dates from observation dates. Read docs/ARCHITECTURE.md and docs/OPERATIONS.md before changing lifecycle behavior.
