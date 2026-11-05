"""Opt-in private-chat commands. No group commands or remote administration."""

import re
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone

from tower.health import status_text
from tower.keyboards import BUTTON_COMMANDS, MAIN_KEYBOARD

BOT_COMMANDS = [
    {"command": command, "description": description}
    for command, description in (
        ("start", "Subscribe or resume with your saved preferences"),
        ("menu", "Open the main menu"),
        ("guide", "Learn how alerts and filters work"),
        ("all", "Turn on alerts and clear every filter"),
        ("help", "Show commands and examples"),
        ("watch", "Set program watchlist: /watch handle or /watch all"),
        ("types", "Set asset types: /types URL WILDCARD or /types all"),
        ("bounties", "Bounty program filter: /bounties on or /bounties off"),
        ("pause", "Pause alerts without replaying missed changes"),
        ("resume", "Resume future alerts with saved preferences"),
        ("settings", "Show your subscription preferences"),
        ("recent", "Show five recent observed changes"),
        ("program", "Show cached program details: /program handle"),
        ("status", "Show monitoring freshness and delivery status"),
        ("privacy", "Read data retention and deletion information"),
        ("delete_me", "Erase your subscription and queued delivery records"),
    )
]

HELP = """HackerOne Tower — public program change notifications

Use the buttons below, or send one command per message.

/start — subscribe or resume with your saved filters
/menu — open the main menu
/guide — a short guide to alerts and filters
/all — turn alerts on and clear every filter
/settings — see your alert preferences
/watch handle1 handle2 — replace your watchlist (up to 30)
/watch all — clear only the program watchlist
/types URL WILDCARD — choose asset types
/types all — clear only the asset-type filter
/bounties on — programs offering bounties only
/bounties off — include paid and unpaid programs
/pause — pause and cancel pending alerts
/resume — future alerts with your saved filters
/recent [handle] — five recent public changes
/program handle — cached details and the official policy
/status — coverage, freshness, and delivery health
/privacy — what is stored
/delete_me — erase your subscription and delivery records (no reply retained)

First observed is not a launch date. Check the current policy and permission
before testing. This service monitors public information; it does not test assets."""

GUIDE = """Quick guide

1. Tap Watch everything for all public-program alerts. This clears your filters.
2. Each alert names the program, asset and change. Open its HackerOne link for
the current scope, rules and bounty eligibility.
3. New program found means first seen by Tower, not a confirmed launch.
Detected is when Tower noticed the change. Updates are not instant.

My settings shows your filters. Monitoring status shows freshness.
Recent changes lists the latest five public changes.
Pause alerts stops messages; Resume alerts sends future changes only.

Use /help for individual commands."""

PRIVACY = """Tower stores your Telegram chat ID, subscription preferences, and delivery
records. It does not store your name, username, or command history.
Public history is retained for the configured retention period.
/delete_me removes your subscription and delivery records from this service.
Telegram's copies and operator backups follow their own retention policies."""

MULTI_COMMAND_HELP = """Send one command per message. Nothing was changed.
To turn alerts on and clear every filter in one action, tap Watch everything
or send /all. Use /help for individual commands."""

TEXT_HELP = """Choose a button below, or send /help to see the commands.
Send /start to subscribe with your saved preferences, or tap Watch everything
to turn alerts on and clear every filter. Your settings have not changed."""

EVENT_LABELS = {
    "program_discovered": "New public program found",
    "program_updated": "Program updated",
    "scope_added": "New asset",
    "scope_updated": "Asset updated",
    "scope_removed": "Asset removed",
    "exclusion_added": "Exclusion added",
    "exclusion_updated": "Exclusion changed",
    "exclusion_removed": "Exclusion removed",
}

_COMMAND_TOKEN = re.compile(r"(?<!\S)/[A-Za-z][A-Za-z0-9_]*(?:@[A-Za-z0-9_]+)?(?=\s|$)")


def _multiple_commands(text: str) -> bool:
    return len(_COMMAND_TOKEN.findall(text)) > 1


def _settings_text(subscription: dict | None) -> str:
    filters = subscription["filters"] if subscription else {}
    active = "On" if subscription and subscription["active"] else "Paused"
    if subscription is None:
        active = "Not subscribed — use /start or Watch everything"
    handles = filters.get("handles", [])
    asset_types = filters.get("asset_types", [])
    bounties_only = filters.get("bounties_only", False)
    lines = [
        "Your alert settings",
        f"Alerts: {active}",
        "Programs: " + (", ".join(handles) if handles else "All observed public programs"),
        "Asset types: " + (", ".join(t.upper() for t in asset_types) if asset_types else "All"),
        "Bounty preference: "
        + ("Programs offering bounties only" if bounties_only else "Paid and unpaid programs"),
    ]
    if handles or asset_types or bounties_only:
        lines.extend(
            [
                "",
                "Some new programs may be hidden by these filters.",
                "Tap Watch everything to reset every filter and turn alerts on.",
            ]
        )
    if bounties_only:
        lines.append("This is a program filter; individual assets may not qualify for bounties.")
    if subscription and not subscription["active"]:
        lines.append("Your filters are saved. Resume alerts applies them to future changes only.")
    return "\n".join(lines)


def _plain_label(value: str) -> str:
    """Keep an untrusted public name on one readable line."""
    value = "".join(
        char for char in value if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    value = " ".join(value.split())
    return value if len(value) <= 120 else value[:119] + "…"


def reply_parts(text: str) -> list[str]:
    """Bound replies in Telegram's UTF-16 units without truncating preferences."""
    chunks, current = [], []
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 3500:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        chunks.append("".join(current))
    if len(chunks) <= 1:
        return chunks
    return [f"Reply part {index}/{len(chunks)}\n{chunk}" for index, chunk in enumerate(chunks, 1)]


class Bot:
    def __init__(self, store, clock=time.monotonic, settings=None):
        self.store = store
        self.clock = clock
        self.settings = settings
        self._command_limits: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def _allow_command(self, chat_id: str) -> bool:
        now = self.clock()
        tokens, previous = self._command_limits.pop(chat_id, (8.0, now))
        tokens = min(8.0, tokens + max(0.0, now - previous) / 3.0)
        allowed = tokens >= 1.0
        self._command_limits[chat_id] = (tokens - 1.0 if allowed else tokens, now)
        if len(self._command_limits) > 10000:
            self._command_limits.popitem(last=False)
        return allowed

    def handle(self, update: dict) -> None:
        if not isinstance(update, dict):
            return
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < 0:
            return
        offset = int(self.store.get_meta("telegram_offset", "0"))
        if update_id < offset:
            return
        message = update.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        text = message.get("text", "") if isinstance(message, dict) else ""
        if not isinstance(chat, dict):
            chat = {}
        if chat.get("type") == "private" and type(chat.get("id")) is int and chat["id"] > 0:
            if isinstance(text, str) and text.strip():
                chat_id = str(chat["id"])
                text = BUTTON_COMMANDS.get(text.strip(), text.strip())
                multiple_commands = _multiple_commands(text)
                deletion = (
                    not multiple_commands
                    and text.split(maxsplit=1)[0].split("@", 1)[0].lower() == "/delete_me"
                )
                if deletion or self._allow_command(chat_id):
                    # Inspect the whole message before truncation or any preference/deletion change.
                    response = (
                        MULTI_COMMAND_HELP
                        if multiple_commands
                        else self.command(chat_id, text[:4096])
                    )
                    if deletion:
                        self._command_limits.pop(chat_id, None)
                    if response:
                        parts = reply_parts(response)
                        for part, reply in enumerate(parts, 1):
                            self.store.queue_text(
                                chat_id,
                                reply,
                                f"command:{update_id}:{part}",
                                reply_markup=MAIN_KEYBOARD if part == len(parts) else None,
                            )
        self.store.set_meta("telegram_offset", str(update_id + 1))

    def command(self, chat_id: str, text: str) -> str:
        if _multiple_commands(text):
            return MULTI_COMMAND_HELP
        text = BUTTON_COMMANDS.get(text.strip(), text.strip())
        parts = text.split()
        if not parts or not parts[0].startswith("/"):
            return TEXT_HELP
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        sub = self.store.get_subscription(chat_id)
        filters = dict(sub["filters"]) if sub else {}
        active = bool(sub["active"]) if sub else True
        if command == "/help":
            return HELP
        if command == "/guide":
            return GUIDE
        if command == "/menu":
            return (
                "HackerOne Tower\n\n"
                "Recent changes — the latest five public changes\n"
                "Monitoring status — coverage and freshness\n"
                "My settings — your saved filters\n"
                "Quick guide — how to read alerts and choose coverage\n\n"
                "Watch everything turns alerts on and clears every filter.\n"
                "Bounty programs keeps your other filters.\n"
                "Pause alerts and Resume alerts keep your preferences.\n\n"
                + (
                    "Alerts are on."
                    if sub and sub["active"]
                    else "Alerts are paused."
                    if sub
                    else "You have not subscribed yet. Use /start."
                )
            )
        if command == "/start":
            self.store.set_subscription(chat_id, True, filters)
            coverage = self.store.status(time.time())
            if coverage["last_catalog_at"] is None:
                coverage_text = "Coverage: waiting for the first public program catalog."
            else:
                coverage_text = (
                    f"Coverage: {coverage['active_programs']:,} public programs in the catalog; "
                    f"scope details collected for {coverage['baselined_programs']:,}.\n"
                    "These are cached observations. Tap Monitoring status for freshness."
                )
            return (
                "Welcome to HackerOne Tower\n"
                "Alerts are on for future observed public changes. Your saved filters are kept.\n\n"
                + _settings_text(self.store.get_subscription(chat_id))
                + "\n\n"
                + coverage_text
                + "\n\n"
                "Tap Recent changes to explore, or Quick guide to learn how alerts work.\n"
                "Watch everything clears every filter and turns alerts on.\n"
                "Coverage is limited to public programs returned by the HackerOne API. "
                "First observed is not a launch date; updates are not instant."
            )
        if command == "/all":
            if args:
                return "Send /all on its own to turn alerts on and clear every filter."
            self.store.set_subscription(
                chat_id, True, {"handles": [], "asset_types": [], "bounties_only": False}
            )
            return (
                "Alerts are on. Every filter has been cleared for future changes.\n\n"
                + _settings_text(self.store.get_subscription(chat_id))
                + "\n\nCoverage includes public programs returned by the HackerOne API."
            )
        if command == "/privacy":
            return PRIVACY
        if command == "/delete_me":
            self.store.delete_subscription(chat_id)
            return ""
        if command == "/status":
            return status_text(self.store, self.settings)
        if command in {"/pause", "/resume"}:
            self.store.set_subscription(chat_id, command == "/resume", filters)
            return (
                "Alerts paused. Pending alerts are cancelled. Your filters are saved.\n"
                "Changes missed while paused will not be replayed. Tap Resume alerts when ready."
                if command == "/pause"
                else "Alerts resumed for future changes only. Missed changes are not replayed.\n\n"
                + _settings_text(self.store.get_subscription(chat_id))
            )
        if command == "/settings":
            return _settings_text(sub)
        if command in {"/watch", "/types", "/bounties"}:
            if command == "/bounties":
                if len(args) != 1 or args[0].lower() not in {"on", "off"}:
                    return "Use /bounties on or /bounties off."
                filters["bounties_only"] = args[0].lower() == "on"
            else:
                key = "handles" if command == "/watch" else "asset_types"
                if (
                    not args
                    or len(args) > 30
                    or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", a) for a in args)
                ):
                    return f"Use {command} followed by up to 30 names, or {command} all."
                filters[key] = (
                    []
                    if len(args) == 1 and args[0].lower() == "all"
                    else sorted({a.lower() if key == "handles" else a.upper() for a in args})
                )
            self.store.set_subscription(chat_id, active, filters)
            return "Preferences saved for future changes.\n\n" + _settings_text(
                self.store.get_subscription(chat_id)
            )
        if command == "/recent":
            if len(args) > 1 or (args and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args[0])):
                return "Use /recent or /recent handle."
            events = self.store.recent_events(5, args[0].lower() if args else None)
            if not events:
                return (
                    "Recent public changes\n\nNo changes recorded for this selection yet. "
                    "The initial collection is silent. Tap Monitoring status for coverage."
                )
            lines = ["Recent public changes (up to five)", "Independent of your alert filters."]
            for event in events:
                observed = datetime.fromisoformat(event["observed_at"]).astimezone(timezone.utc)
                lines.extend(
                    [
                        "",
                        f"{EVENT_LABELS.get(event['kind'], 'Public program change')} | "
                        f"@{event['handle']}",
                    ]
                )
                if event["kind"].startswith("scope_"):
                    record = event.get("before" if event["kind"] == "scope_removed" else "after")
                    asset = record.get("asset_identifier") if isinstance(record, dict) else None
                    if isinstance(asset, str) and asset.strip():
                        lines.append(f"Asset: {_plain_label(asset)}")
                lines.extend(
                    [
                        f"Observed: {observed:%Y-%m-%d %H:%M UTC}",
                        f"https://hackerone.com/{event['handle']}",
                    ]
                )
            lines.extend(["", "Observed dates are not launch dates. Check current source details."])
            return "\n".join(lines)
        if command == "/program":
            if len(args) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args[0]):
                return "Use /program handle."
            snapshot = self.store.get_program(args[0].lower())
            if not snapshot:
                return (
                    "No cached details available for that handle among monitored public programs. "
                    "Check the handle or tap Monitoring status for coverage."
                )
            program = snapshot["program"]
            name = _plain_label(program.get("name") or program["handle"]) or program["handle"]
            bounty = program.get("offers_bounties")
            bounty_text = "Yes" if bounty is True else "No" if bounty is False else "Not provided"
            return (
                f"{name} (@{program['handle']})\n"
                "Visibility at last collection: Public\n"
                f"Program offers bounties: {bounty_text}\n"
                f"Scope entries: {len(snapshot['scopes']):,}\n"
                f"Exclusion entries: {len(snapshot['exclusions']):,}\n"
                f"Official program: https://hackerone.com/{program['handle']}\n\n"
                "These are cached details. Check the current policy, scope, exclusions, and "
                "asset-level bounty eligibility at the source before choosing a program. "
                "Tap Monitoring status for freshness."
            )
        return (
            "Unknown command. Use /help or choose a button below. Your settings have not changed."
        )
