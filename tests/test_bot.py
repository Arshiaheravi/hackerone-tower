"""Bot commands use the real subscription and delivery store."""

from copy import deepcopy

import pytest

from tower.bot import BOT_COMMANDS, Bot, reply_parts
from tower.keyboards import MAIN_KEYBOARD
from tower.store import Store


def update(update_id, text, chat_id=100, chat_type="private"):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": chat_type, "username": "never_store_me"},
            "from": {"first_name": "Also never store me"},
            "text": text,
        },
    }


def snapshot(program_id="1", handle="example"):
    return {
        "program": {
            "id": program_id,
            "handle": handle,
            "state": "public_mode",
            "name": handle,
            "offers_bounties": True,
        },
        "scopes": [{"id": "10", "asset_identifier": "example.com", "asset_type": "URL"}],
        "exclusions": [],
    }


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "bot.db")
    yield value
    value.close()


def drain(store):
    messages = []
    while pending := store.pending_deliveries(10000):
        for item in pending:
            messages.append(item)
            store.delivered(item["id"], len(messages), 10000)
    return messages


def test_subscribe_watch_type_bounty_filters_apply_to_event_delivery(store):
    bot = Bot(store)
    first, other = snapshot(), snapshot("2", "other")
    store.sync_catalog([first["program"], other["program"]], 1000)
    store.apply_snapshot(first, 1000, 60)
    store.apply_snapshot(other, 1000, 60)
    for number, command in enumerate(["/start", "/watch EXAMPLE", "/types URL", "/bounties on"], 1):
        bot.handle(update(number, command))
    subscription = store.get_subscription("100")
    assert subscription["active"] is True
    assert subscription["filters"] == {
        "handles": ["example"],
        "asset_types": ["url"],
        "bounties_only": True,
    }
    drain(store)
    for value in (first, other):
        value["scopes"][0]["instruction"] = "A changed instruction"
        store.apply_snapshot(value, 1060, 60)
    alerts = drain(store)
    assert alerts
    assert all(item["event_id"] for item in alerts)
    assert all(item["text"].endswith("https://hackerone.com/example") for item in alerts)
    assert all("(@other)" not in item["text"] for item in alerts)


def test_replayed_update_is_idempotent_and_offset_survives_restart(tmp_path):
    path = tmp_path / "restart.db"
    store = Store(path)
    bot = Bot(store)
    bot.handle(update(42, "/start"))
    assert store.get_meta("telegram_offset") == "43"
    initial_count = store.status(1000)["pending_deliveries"]
    bot.handle(update(42, "/pause"))
    assert store.get_subscription("100")["active"] is True
    assert store.status(1000)["pending_deliveries"] == initial_count
    store.close()
    reopened = Store(path)
    Bot(reopened).handle(update(42, "/pause"))
    assert reopened.get_subscription("100")["active"] is True
    assert reopened.get_meta("telegram_offset") == "43"
    reopened.close()


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_nonprivate_commands_ignored_but_offset_acknowledged(store, chat_type):
    Bot(store).handle(update(1, "/start", chat_type=chat_type))
    assert store.get_subscription("100") is None
    assert store.pending_deliveries(1000) == []
    assert store.get_meta("telegram_offset") == "2"


def test_normal_messages_and_help_do_not_implicitly_subscribe(store):
    bot = Bot(store)
    bot.handle(update(1, "hello"))
    assert "Choose a button" in drain(store)[0]["text"]
    bot.handle(update(2, "/help"))
    assert store.get_subscription("100") is None
    assert "/start" in store.pending_deliveries(1000)[0]["text"]


def test_pause_resume_and_filter_reset_keep_explicit_preferences(store):
    bot = Bot(store)
    for number, command in enumerate(["/start", "/watch example", "/pause", "/types URL"], 1):
        bot.handle(update(number, command))
    assert store.get_subscription("100")["active"] is False
    bot.handle(update(5, "/resume"))
    assert store.get_subscription("100")["active"] is True
    assert store.get_subscription("100")["filters"]["handles"] == ["example"]
    bot.handle(update(6, "/watch all"))
    bot.handle(update(7, "/types all"))
    assert store.get_subscription("100")["filters"]["handles"] == []
    assert store.get_subscription("100")["filters"]["asset_types"] == []


def test_delete_me_erases_subscription_delivery_history_without_reply(store):
    bot = Bot(store)
    bot.handle(update(1, "/start"))
    drain(store)
    bot.handle(update(2, "/watch example"))
    bot.handle(update(3, "/delete_me"))
    assert store.get_subscription("100") is None
    assert store.pending_deliveries(10000) == []
    assert store.status(10000)["pending_deliveries"] == 0
    assert store.get_meta("telegram_offset") == "4"
    dump = "\n".join(store._db.iterdump())
    assert "never_store_me" not in dump
    assert "Also never store me" not in dump
    assert "reply:100:" not in dump


def test_invalid_filters_do_not_change_subscription(store):
    bot = Bot(store)
    bot.handle(update(1, "/start"))
    expected = deepcopy(store.get_subscription("100"))
    for number, command in enumerate(
        ["/watch ../private", "/watch", "/types <script>", "/bounties maybe"], 2
    ):
        bot.handle(update(number, command))
        assert store.get_subscription("100") == expected


def test_program_and_recent_commands_hide_inactive_programs(store):
    value = snapshot()
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    value["scopes"][0]["instruction"] = "Changed"
    store.apply_snapshot(value, 1060, 60)
    bot = Bot(store)
    assert "https://hackerone.com/example" in bot.command("100", "/program example")
    assert "Asset updated" in bot.command("100", "/recent example")
    store.sync_catalog([], 1120)
    assert "No cached details" in bot.command("100", "/program example")
    assert "No changes recorded" in bot.command("100", "/recent example")


def test_maximum_combined_filters_and_settings_fit_telegram_delivery(store):
    bot = Bot(store)
    handles = [f"h{number:02d}" + "x" * 97 for number in range(30)]
    types = [f"t{number:02d}" + "y" * 97 for number in range(30)]
    bot.handle(update(1, "/watch " + " ".join(handles)))
    bot.handle(update(2, "/types " + " ".join(types)))
    bot.handle(update(3, "/settings"))
    assert store.get_meta("telegram_offset") == "4"
    assert len(store.get_subscription("100")["filters"]["handles"]) == 30
    assert len(store.get_subscription("100")["filters"]["asset_types"]) == 30
    messages = drain(store)
    assert len(messages) > 3
    assert all(len(item["text"].encode("utf-16-le")) // 2 <= 4096 for item in messages)


@pytest.mark.parametrize("bad_chat", [None, [], "invalid"])
def test_malformed_chat_does_not_stop_command_processing(store, bad_chat):
    message = update(1, "/start")
    message["message"]["chat"] = bad_chat
    Bot(store).handle(message)
    assert store.get_subscription("100") is None
    assert store.get_meta("telegram_offset") == "2"


def test_unicode_reply_splitting_is_lossless_and_bounded():
    text = "\U0001f916" * 4000
    parts = reply_parts(text)
    assert len(parts) > 1
    assert "".join(part.split("\n", 1)[1] for part in parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)


def test_partially_queued_reply_replays_without_duplicate_parts(store, monkeypatch):
    bot = Bot(store)
    long_response = "x" * 8000
    monkeypatch.setattr(bot, "command", lambda *_: long_response)
    original = store.queue_text
    calls = 0

    def interrupted(chat_id, text, key, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash")
        original(chat_id, text, key, **kwargs)

    monkeypatch.setattr(store, "queue_text", interrupted)
    with pytest.raises(RuntimeError):
        bot.handle(update(1, "/settings"))
    assert store.get_meta("telegram_offset") is None
    monkeypatch.setattr(store, "queue_text", original)
    bot.handle(update(1, "/settings"))
    assert len(drain(store)) == len(reply_parts(long_response))


def test_command_burst_is_bounded_per_chat_and_refills_without_queuing_rejections(store):
    now = [0.0]
    bot = Bot(store, clock=lambda: now[0])
    bot.handle(update(1, "/start"))
    for number in range(2, 9):
        bot.handle(update(number, "/settings"))
    assert store.status(0)["pending_deliveries"] == 8
    bot.handle(update(9, "/pause"))
    assert store.get_meta("telegram_offset") == "10"
    assert store.get_subscription("100")["active"] is True
    assert store.status(0)["pending_deliveries"] == 8
    # Another chat retains its own burst allowance.
    bot.handle(update(10, "/start", chat_id=200))
    assert store.get_subscription("200")["active"] is True
    assert store.status(0)["pending_deliveries"] == 9
    now[0] = 2.5
    bot.handle(update(11, "/pause"))
    assert store.get_subscription("100")["active"] is True
    now[0] = 3.0
    bot.handle(update(12, "/pause"))
    assert store.get_subscription("100")["active"] is False
    bot.handle(update(13, "/resume"))
    assert store.get_subscription("100")["active"] is False
    now[0] = 6.0
    bot.handle(update(14, "/resume"))
    assert store.get_subscription("100")["active"] is True
    assert store.get_meta("telegram_offset") == "15"
    assert store.status(0)["pending_deliveries"] == 11


def test_privacy_deletion_bypasses_throttle_and_erases_throttle_entry(store):
    bot = Bot(store, clock=lambda: 0.0)
    bot.handle(update(1, "/start"))
    for number in range(2, 9):
        bot.handle(update(number, "/help"))
    assert "100" in bot._command_limits
    bot.handle(update(9, "/delete_me@tower_bot"))
    assert store.get_subscription("100") is None
    assert store.status(0)["pending_deliveries"] == 0
    assert "100" not in bot._command_limits
    assert store.get_meta("telegram_offset") == "10"
    bot.handle(update(10, "/start"))
    assert store.get_subscription("100")["active"] is True


def test_command_throttle_memory_uses_bounded_least_recently_used_entries(store):
    bot = Bot(store, clock=lambda: 0.0)
    for number in range(10000):
        assert bot._allow_command(str(number))
    assert bot._allow_command("0")
    assert bot._allow_command("new-chat")
    assert len(bot._command_limits) == 10000
    assert "0" in bot._command_limits
    assert "1" not in bot._command_limits


@pytest.mark.parametrize(
    ("button", "expected_text"),
    [
        ("Recent changes", "Recent public changes"),
        ("Monitoring status", "HackerOne Tower |"),
        ("My settings", "Your alert settings"),
        ("Quick guide", "Quick guide"),
        ("Watch everything", "Every filter has been cleared"),
        ("Bounty programs", "Programs offering bounties only"),
        ("Pause alerts", "Alerts paused"),
        ("Resume alerts", "Alerts resumed"),
    ],
)
def test_menu_buttons_are_usable_in_private_chat_with_persistent_keyboard(
    store, button, expected_text
):
    Bot(store).handle(update(1, button))
    messages = drain(store)
    assert len(messages) == 1
    assert expected_text in messages[0]["text"]
    assert messages[0]["reply_markup"] == MAIN_KEYBOARD


@pytest.mark.parametrize("command", ["/menu", "/guide", "/help", "/settings", "hello"])
def test_readonly_navigation_does_not_resume_or_replace_saved_preferences(store, command):
    filters = {"handles": ["example"], "asset_types": ["url"], "bounties_only": True}
    store.set_subscription("100", False, filters)
    expected = deepcopy(store.get_subscription("100"))
    Bot(store).handle(update(1, command))
    assert drain(store)
    assert store.get_subscription("100") == expected


def test_start_resumes_saved_filters_and_explains_partial_cached_coverage(store):
    first, other = snapshot(), snapshot("2", "other")
    store.sync_catalog([first["program"], other["program"]], 1000)
    store.apply_snapshot(first, 1000, 60)
    filters = {"handles": ["example"], "asset_types": ["url"], "bounties_only": True}
    store.set_subscription("100", False, filters)
    response = Bot(store).command("100", "/start")
    assert store.get_subscription("100") == {
        "chat_id": "100",
        "active": True,
        "filters": filters,
    }
    assert "2 public programs" in response
    assert "scope details collected for 1" in response
    assert "cached observations" in response
    assert "Some new programs may be hidden" in response
    assert "First observed is not a launch date" in response
    assert "updates are not instant" in response


def test_all_resets_every_filter_resumes_and_allows_unmatched_new_public_program(store):
    store.sync_catalog([], 1000)
    store.set_subscription(
        "100", False, {"handles": ["example"], "asset_types": ["url"], "bounties_only": True}
    )
    Bot(store).handle(update(1, "Watch everything"))
    response = drain(store)[0]["text"]
    subscription = store.get_subscription("100")
    assert subscription["active"] is True
    assert subscription["filters"] == {
        "handles": [],
        "asset_types": [],
        "bounties_only": False,
    }
    assert "Every filter has been cleared" in response
    assert "Some new programs may be hidden" not in response
    new = snapshot("2", "new-program")
    new["program"]["offers_bounties"] = False
    new["scopes"][0]["asset_type"] = "HARDWARE"
    store.sync_catalog([new["program"]], 1060)
    store.apply_snapshot(new, 1060, 60)
    alerts = drain(store)
    assert alerts
    assert all(item["event_id"] for item in alerts)
    assert all(item["text"].endswith("https://hackerone.com/new-program") for item in alerts)


def test_settings_are_human_readable_without_internal_or_personal_identifiers(store):
    chat_id = "900001234"
    store.set_subscription(
        chat_id,
        False,
        {"handles": ["example", "other"], "asset_types": ["url"], "bounties_only": True},
    )
    response = Bot(store).command(chat_id, "/settings")
    assert "Alerts: Paused" in response
    assert "Programs: example, other" in response
    assert "Asset types: URL" in response
    assert "Bounty preference: Programs offering bounties only" in response
    assert "individual assets may not qualify" in response
    assert "future changes only" in response
    for internal in (chat_id, "chat_id", "asset_types", "bounties_only", "{", "}"):
        assert internal not in response


def test_filter_confirmations_show_other_saved_filters_and_discovery_warning(store):
    bot = Bot(store)
    bot.command("100", "/watch example")
    bot.command("100", "/types URL")
    response = bot.command("100", "/bounties on")
    assert "Programs: example" in response
    assert "Asset types: URL" in response
    assert "Some new programs may be hidden" in response
    response = bot.command("100", "/watch all")
    assert "Programs: All observed public programs" in response
    assert "Asset types: URL" in response
    assert "Programs offering bounties only" in response
    assert "Some new programs may be hidden" in response
    bot.command("100", "/types all")
    response = bot.command("100", "/bounties off")
    assert "Some new programs may be hidden" not in response


@pytest.mark.parametrize(
    "text",
    [
        "/start\n/watch other",
        "/all\n/types URL",
        "/pause\n/resume",
        "/delete_me@tower_bot\n/start",
        "/all /bounties on",
        "/delete_me " + "x" * 4096 + "\n/start",
    ],
)
def test_pasted_command_batches_are_rejected_before_any_subscription_mutation(store, text):
    store.set_subscription(
        "100", False, {"handles": ["example"], "asset_types": ["url"], "bounties_only": True}
    )
    before = deepcopy(store.get_subscription("100"))
    bot = Bot(store, clock=lambda: 0.0)
    bot.handle(update(1, text))
    assert store.get_subscription("100") == before
    messages = drain(store)
    assert len(messages) == 1
    assert "Send one command per message" in messages[0]["text"]
    assert "Nothing was changed" in messages[0]["text"]
    assert "Watch everything" in messages[0]["text"]
    assert "100" in bot._command_limits
    assert store.get_meta("telegram_offset") == "2"


def test_direct_command_batch_does_not_create_subscription(store):
    response = Bot(store).command("100", "/start\n/all")
    assert "Nothing was changed" in response
    assert store.get_subscription("100") is None


def test_unknown_input_is_not_echoed_and_only_exact_buttons_change_preferences(store):
    bot = Bot(store)
    secret_text = "private-value-do-not-echo-012345"
    bot.handle(update(1, secret_text))
    bot.handle(update(2, "/unknown " + secret_text))
    bot.handle(update(3, "Watch everything now"))
    messages = drain(store)
    assert len(messages) == 3
    assert all(secret_text not in item["text"] for item in messages)
    assert store.get_subscription("100") is None
    assert secret_text not in "\n".join(store._db.iterdump())


def test_menu_buttons_in_groups_do_not_reply_or_subscribe(store):
    Bot(store).handle(update(1, "Watch everything", chat_type="group"))
    assert store.get_subscription("100") is None
    assert drain(store) == []
    assert store.get_meta("telegram_offset") == "2"


def test_keyboard_is_attached_only_to_last_part_and_survives_replayed_update(store, monkeypatch):
    bot = Bot(store)
    monkeypatch.setattr(bot, "command", lambda *_: "x" * 8000)
    bot.handle(update(1, "/settings"))
    bot.handle(update(1, "/settings"))
    messages = drain(store)
    assert len(messages) == 3
    assert [item["reply_markup"] for item in messages] == [None, None, MAIN_KEYBOARD]


def test_pause_cancels_pending_alerts_and_resume_never_replays_missed_changes(store):
    value = snapshot()
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    bot = Bot(store)
    bot.command("100", "/start")
    value["scopes"][0]["instruction"] = "First change"
    store.apply_snapshot(value, 1060, 60)
    assert any(item["event_id"] for item in store.pending_deliveries(1060))
    response = bot.command("100", "/pause")
    assert "Pending alerts are cancelled" in response
    assert drain(store) == []
    value["scopes"][0]["instruction"] = "Missed while paused"
    store.apply_snapshot(value, 1120, 60)
    response = bot.command("100", "/resume")
    assert "future changes only" in response
    assert "not replayed" in response
    assert drain(store) == []
    value["scopes"][0]["instruction"] = "New after resume"
    store.apply_snapshot(value, 1180, 60)
    assert any(item["event_id"] for item in drain(store))


def test_recent_shows_five_human_labels_and_observed_dates_independent_of_filters(store):
    value = snapshot()
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    for number in range(7):
        value["scopes"][0]["instruction"] = f"Change {number}"
        store.apply_snapshot(value, 1060 + number * 60, 60)
    store.set_subscription("100", True, {"handles": ["different-program"]})
    response = Bot(store).command("100", "/recent")
    assert response.count("Asset updated") == 5
    assert response.count("Asset: example.com") == 5
    assert response.count("Observed: 1970-01-01") == 5
    assert "Independent of your alert filters" in response
    assert "scope_updated" not in response
    assert "not launch dates" in response


@pytest.mark.parametrize("kind", ["scope_added", "scope_updated", "scope_removed"])
def test_recent_shows_the_affected_asset_as_one_bounded_line(store, kind):
    value = snapshot()
    asset = "new.example.test\n\u202e" + "x" * 180
    if kind == "scope_added":
        value["scopes"] = []
    elif kind == "scope_removed":
        value["scopes"][0]["asset_identifier"] = asset
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    if kind == "scope_removed":
        value["scopes"] = []
    elif kind == "scope_added":
        value["scopes"] = [{"id": "10", "asset_identifier": asset, "asset_type": "URL"}]
    else:
        value["scopes"][0]["asset_identifier"] = asset
    store.apply_snapshot(value, 1060, 60)

    response = Bot(store).command("100", "/recent")
    asset_lines = [line for line in response.splitlines() if line.startswith("Asset: ")]
    assert len(asset_lines) == 1
    assert asset_lines[0].startswith("Asset: new.example.test ")
    assert asset_lines[0].endswith("…")
    assert len(asset_lines[0]) <= 127
    assert "\u202e" not in response
    assert "https://hackerone.com/example" in response


@pytest.mark.parametrize(
    ("bounty_value", "expected"), [(True, "Yes"), (False, "No"), (None, "Not provided")]
)
def test_program_shows_useful_cached_details_and_current_policy_link(store, bounty_value, expected):
    value = snapshot()
    value["program"].update({"name": "Example Company", "offers_bounties": bounty_value})
    value["exclusions"] = [{"id": "99", "asset_identifier": "private.example.com"}]
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    response = Bot(store).command("100", "/program EXAMPLE")
    assert "Example Company (@example)" in response
    assert "Visibility at last collection: Public" in response
    assert f"Program offers bounties: {expected}" in response
    assert "Scope entries: 1" in response
    assert "Exclusion entries: 1" in response
    assert "https://hackerone.com/example" in response
    assert "cached details" in response
    assert "asset-level bounty eligibility" in response
    assert "private.example.com" not in response


def test_program_name_cannot_inject_extra_lines_or_bidi_controls(store):
    value = snapshot()
    value["program"]["name"] = "Example\n\r\u202eFake status" + "x" * 500
    store.sync_catalog([value["program"]], 1000)
    store.apply_snapshot(value, 1000, 60)
    response = Bot(store).command("100", "/program example")
    assert "\r" not in response
    assert "\u202e" not in response
    assert len(response.splitlines()[0]) <= 131
    assert response.splitlines()[1] == "Visibility at last collection: Public"


def test_status_receives_config_without_using_monotonic_clock_as_epoch(store, monkeypatch):
    import tower.bot

    settings = object()
    received = []
    monkeypatch.setattr(
        tower.bot,
        "status_text",
        lambda actual_store, actual_settings: received.append((actual_store, actual_settings))
        or "Readable health",
    )
    response = Bot(store, clock=lambda: 0.0, settings=settings).command("100", "/status")
    assert response == "Readable health"
    assert received == [(store, settings)]


def test_command_menu_retains_legacy_commands_and_adds_beginner_navigation():
    commands = {entry["command"] for entry in BOT_COMMANDS}
    assert commands == {
        "start",
        "menu",
        "guide",
        "all",
        "help",
        "watch",
        "types",
        "bounties",
        "pause",
        "resume",
        "settings",
        "recent",
        "program",
        "status",
        "privacy",
        "delete_me",
    }
