"""Private menu -> durable settings/outbox -> mocked Telegram acceptance."""

import json
from copy import deepcopy

import httpx

from tower.bot import Bot
from tower.keyboards import MAIN_KEYBOARD
from tower.service import Dispatcher
from tower.store import Store
from tower.telegram import TelegramClient


def test_watch_everything_menu_survives_retry_restart_and_delivers_all_public_change_types(
    tmp_path,
):
    path = tmp_path / "menu-workflow.db"
    store = Store(path)
    first = {
        "program": {
            "id": "1",
            "handle": "fixture",
            "name": "Fixture",
            "state": "public_mode",
            "offers_bounties": True,
        },
        "scopes": [{"id": "10", "asset_identifier": "example.test", "asset_type": "URL"}],
        "exclusions": [],
    }
    store.sync_catalog([first["program"]], 1000)
    assert store.apply_snapshot(first, 1000, 900) == []
    store.set_subscription(
        "123",
        False,
        {
            "handles": ["old_shortlist"],
            "asset_types": ["url"],
            "bounties_only": True,
        },
    )
    update = {
        "update_id": 1,
        "message": {"chat": {"type": "private", "id": 123}, "text": "Watch everything"},
    }
    Bot(store).handle(update)
    subscription = store.get_subscription("123")
    assert subscription["active"] is True
    assert subscription["filters"] == {"handles": [], "asset_types": [], "bounties_only": False}
    now = [1000]
    requests = []
    accepted = []

    def handler(request):
        assert request.url.host == "api.telegram.org"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        accepted.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(accepted)}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        telegram = TelegramClient("123:fixture_token", client=http)
        dispatcher = Dispatcher(store, telegram, clock=lambda: now[0])
        dispatcher.tick()
        assert not accepted
        assert store.status(now[0])["pending_deliveries"] == 1
        store.close()
        store = Store(path)
        try:
            Bot(store).handle(update)
            assert store.status(now[0])["pending_deliveries"] == 1
            dispatcher = Dispatcher(store, telegram, clock=lambda: now[0])
            now[0] = 1008
            dispatcher.tick()
            assert accepted[0]["reply_markup"] == MAIN_KEYBOARD
            assert requests[0] == accepted[0]
            assert store.status(now[0])["pending_deliveries"] == 0

            # An unpaid program and a different asset type still reach all-public mode.
            second = deepcopy(first)
            second["program"].update(
                id="2", handle="new_fixture", name="New fixture", offers_bounties=False
            )
            second["scopes"] = [
                {
                    "id": "20",
                    "asset_identifier": "example.test.mobile",
                    "asset_type": "ANDROID_APP_ID",
                }
            ]
            store.sync_catalog([first["program"], second["program"]], now[0])
            assert store.apply_snapshot(second, now[0], 900)[0]["kind"] == "program_discovered"
            first["scopes"][0]["instruction"] = "Updated public instructions"
            first["program"]["policy"] = "Updated public policy"
            first["exclusions"] = [
                {"id": "30", "category": "testing", "details": "A public exclusion"}
            ]
            events = store.apply_snapshot(first, now[0], 900)
            assert {event["kind"] for event in events} == {
                "program_updated",
                "scope_updated",
                "exclusion_added",
            }
            first["scopes"].append(
                {"id": "11", "asset_identifier": "new.example.test", "asset_type": "URL"}
            )
            first["exclusions"][0]["details"] = "Revised public exclusion"
            assert {event["kind"] for event in store.apply_snapshot(first, now[0], 900)} == {
                "scope_added",
                "exclusion_updated",
            }
            first["scopes"].pop(0)
            first["exclusions"] = []
            assert {event["kind"] for event in store.apply_snapshot(first, now[0], 900)} == {
                "scope_removed",
                "exclusion_removed",
            }
            for _ in range(60):
                now[0] += 4
                dispatcher.tick()
                if not store.status(now[0])["pending_deliveries"]:
                    break
            assert store.status(now[0])["pending_deliveries"] == 0
            assert store.status(now[0])["dead_deliveries"] == 0
            alerts = accepted[1:]
            assert len(alerts) == 8
            assert all("reply_markup" not in payload for payload in alerts)
            assert all(len(payload["text"].encode("utf-16-le")) // 2 <= 800 for payload in alerts)
            assert all(len(payload["text"].splitlines()) <= 6 for payload in alerts)
            text = "\n".join(payload["text"] for payload in alerts)
            assert "new_fixture" in text and "Instructions updated" in text
            assert "Policy updated" in text and "Exclusion: testing" in text
            assert "A public exclusion" not in text
            assert "Revised public exclusion" not in text
            assert "Updated public instructions" not in text
            assert "Updated public policy" not in text
            assert store.apply_snapshot(first, now[0], 900) == []
            assert store.apply_snapshot(second, now[0], 900) == []
        finally:
            store.close()
