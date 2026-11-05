import json

import httpx
import pytest

from tower.keyboards import BUTTON_COMMANDS, MAIN_KEYBOARD, validate_reply_markup
from tower.telegram import TelegramClient, TelegramError

TOKEN = "123456:unit_test_secret"


def client_for(handler):
    return TelegramClient(TOKEN, httpx.Client(transport=httpx.MockTransport(handler)))


def test_webhook_status_does_not_return_secret_url():
    client = client_for(
        lambda request: httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"url": "https://example.invalid/secret-hook"},
            },
        )
    )
    assert client.get_webhook_status() == {"configured": True}


def test_command_menu_is_installed_and_read_in_private_chat_scope():
    commands = [{"command": "status", "description": "Show monitoring status"}]

    def handler(request):
        payload = json.loads(request.content)
        assert payload["scope"] == {"type": "all_private_chats"}
        if request.url.path.endswith("setMyCommands"):
            assert payload["commands"] == commands
            result = True
        else:
            result = commands
        return httpx.Response(200, json={"ok": True, "result": result})

    client = client_for(handler)
    client.set_my_commands(commands)
    assert client.get_my_commands() == commands


@pytest.mark.parametrize("result", [None, {}, [{"command": "INVALID", "description": "x"}]])
def test_invalid_command_menu_is_rejected_without_returning_response(result):
    client = client_for(lambda request: httpx.Response(200, json={"ok": True, "result": result}))
    with pytest.raises(TelegramError):
        client.get_my_commands()


def test_send_plain_text_without_link_previews_or_markup():
    def handler(request):
        assert request.method == "POST"
        assert request.url.host == "api.telegram.org"
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        body = json.loads(request.content)
        assert body == {
            "chat_id": "@scope_news",
            "text": "<b>Literal policy</b> & _instructions_",
            "link_preview_options": {"is_disabled": True},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    client = client_for(handler)
    assert client.send_message("@scope_news", "<b>Literal policy</b> & _instructions_") == 99


def test_private_chat_menu_sends_expected_reply_keyboard_payload():
    def handler(request):
        payload = json.loads(request.content)
        assert payload == {
            "chat_id": "123456789",
            "text": "Choose an option below.",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": MAIN_KEYBOARD,
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 100}})

    assert (
        client_for(handler).send_message(
            "123456789", "Choose an option below.", reply_markup=MAIN_KEYBOARD
        )
        == 100
    )


def test_explicit_none_keyboard_preserves_channel_message_payload():
    def handler(request):
        payload = json.loads(request.content)
        assert "reply_markup" not in payload
        assert payload["chat_id"] == "-100123456789"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    assert (
        client_for(handler).send_message("-100123456789", "Channel alert", reply_markup=None) == 101
    )


def test_main_menu_labels_map_to_expected_commands():
    labels = [label for row in MAIN_KEYBOARD["keyboard"] for label in row]
    assert [BUTTON_COMMANDS[label] for label in labels] == [
        "/recent",
        "/status",
        "/settings",
        "/guide",
        "/all",
        "/bounties on",
        "/pause",
        "/resume",
    ]
    assert set(labels) == set(BUTTON_COMMANDS)


def test_keyboard_validation_normalizes_text_objects_and_copies_nested_rows():
    original = {
        "keyboard": [[{"text": "Recent changes"}, "Quick guide"]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "selective": False,
        "input_field_placeholder": "Choose an option",
    }
    normalized = validate_reply_markup(original)
    assert normalized["keyboard"] == [["Recent changes", "Quick guide"]]
    original["keyboard"][0][0]["text"] = "changed"
    original["keyboard"][0].append("new button")
    assert normalized["keyboard"] == [["Recent changes", "Quick guide"]]
    normalized["keyboard"][0][1] = "changed normalized copy"
    assert original["keyboard"][0][1] == "Quick guide"
    assert validate_reply_markup(None) is None


@pytest.mark.parametrize(
    "markup",
    [
        {},
        [],
        True,
        "keyboard",
        {"keyboard": []},
        {"keyboard": [[]]},
        {"keyboard": "buttons"},
        {"keyboard": [[""]]},
        {"keyboard": [["   "]]},
        {"keyboard": [[None]]},
        {"keyboard": [[1]]},
        {"keyboard": [[[]]]},
        {"keyboard": [("one",)]},
        {"keyboard": [[{"text": "one", "request_contact": True}]]},
        {"keyboard": [[{"text": "one", "request_location": True}]]},
        {"keyboard": [[{"text": "one", "request_users": {"request_id": 1}}]]},
        {"keyboard": [[{"text": "one", "request_chat": {"request_id": 1}}]]},
        {"keyboard": [[{"text": "one", "web_app": {"url": "https://example.invalid"}}]]},
        {"keyboard": [[{"text": "one", "request_poll": {"type": "quiz"}}]]},
        {"keyboard": [[{"text": "one", "url": "https://example.invalid"}]]},
        {"keyboard": [[{"text": "one", "callback_data": "hidden_action"}]]},
        {"keyboard": [[{"text": "one", "request_managed_bot": {}}]]},
        {"keyboard": [["one"]], "inline_keyboard": []},
        {"keyboard": [["one"]], "remove_keyboard": True},
        {"keyboard": [["one"]], "unknown": "sensitive-input"},
        {"keyboard": [["one"]], "resize_keyboard": 1},
        {"keyboard": [["one"]], "is_persistent": "true"},
        {"keyboard": [["one"]], "selective": None},
        {"keyboard": [["one"]], "one_time_keyboard": []},
        {"keyboard": [["one"]], "input_field_placeholder": ""},
        {"keyboard": [["one"]], "input_field_placeholder": "x" * 65},
        {"keyboard": [["x" * 65]]},
        {"keyboard": [["one"]] * 9},
        {"keyboard": [["one"] * 9]},
        {"keyboard": [["one"] * 8] * 5},
        {"keyboard": [["one\nsecond"]]},
        {"keyboard": [["one\x00"]]},
        {"keyboard": [["one\u202e"]]},
        {"keyboard": [["one\ud800"]]},
    ],
)
def test_keyboard_rejects_unknown_capabilities_bad_shapes_and_oversized_values_before_http(markup):
    client = client_for(lambda request: pytest.fail("Invalid keyboard must not reach Telegram"))
    with pytest.raises(ValueError) as error:
        client.send_message("123456789", "Choose an option", reply_markup=markup)
    assert str(error.value) == "Invalid Telegram reply keyboard"
    assert "sensitive-input" not in repr(error.value)
    assert TOKEN not in repr(error.value)


@pytest.mark.parametrize(
    "destination",
    ["@scope_news", "-100123456789", "-123", "0", "0123", "+123", "123\n", "١٢٣", True],
)
def test_keyboard_requires_a_positive_ascii_private_chat_id(destination):
    client = client_for(lambda request: pytest.fail("Keyboard destination is not a private chat"))
    with pytest.raises(ValueError, match="positive private numeric"):
        client.send_message(destination, "Menu", reply_markup=MAIN_KEYBOARD)


def test_keyboard_unicode_bounds_count_utf16_units():
    assert validate_reply_markup({"keyboard": [["\U0001f600" * 32]]}) is not None
    with pytest.raises(ValueError):
        validate_reply_markup({"keyboard": [["\U0001f600" * 33]]})


def test_telegram_keyboard_rejection_does_not_leak_token_or_reply_body():
    def handler(request):
        assert "reply_markup" in json.loads(request.content)
        return httpx.Response(400, json={"ok": False, "description": f"private details {TOKEN}"})

    with pytest.raises(TelegramError) as error:
        client_for(handler).send_message("123456789", "Menu", reply_markup=MAIN_KEYBOARD)
    assert error.value.permanent is True
    assert TOKEN not in str(error.value)
    assert "private details" not in repr(error.value)


@pytest.mark.parametrize("http_status", [200, 429])
def test_rate_limit_preserves_retry_after_without_retrying(http_status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            http_status,
            json={"ok": False, "error_code": 429, "parameters": {"retry_after": 73}},
        )

    with pytest.raises(TelegramError) as error:
        client_for(handler).send_message("123", "hello")
    assert error.value.retry_after == 73
    assert error.value.permanent is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    "code,permanent", [(400, True), (401, True), (403, True), (409, False), (500, False)]
)
def test_http_and_api_errors_are_safe(code, permanent):
    client = client_for(
        lambda request: httpx.Response(
            200, json={"ok": False, "error_code": code, "description": TOKEN}
        )
    )
    with pytest.raises(TelegramError) as error:
        client.send_message("123", "hello")
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(error.value)
    assert error.value.permanent is permanent


def test_network_timeout_is_ambiguous_and_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"Token in request URL {TOKEN}", request=request)

    with pytest.raises(TelegramError) as error:
        client_for(handler).send_message("123", "hello")
    assert error.value.permanent is False
    assert error.value.__suppress_context__ is True
    assert TOKEN not in str(error.value)
    assert len(calls) == 1


def test_redirect_is_not_followed_even_with_injected_redirect_enabled_client():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://example.org/"})

    client = TelegramClient(
        TOKEN, httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    )
    with pytest.raises(TelegramError):
        client.get_me()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": True}}),
        httpx.Response(503, json={"ok": True, "result": {"message_id": 5}}),
    ],
)
def test_malformed_or_unsuccessful_response_is_rejected(response):
    with pytest.raises(TelegramError):
        client_for(lambda request: response).send_message("123", "hello")


@pytest.mark.parametrize("retry_after", [-1, "50", True, None])
def test_invalid_retry_after_is_not_used(retry_after):
    client = client_for(
        lambda request: httpx.Response(
            429,
            json={"ok": False, "parameters": {"retry_after": retry_after}},
        )
    )
    with pytest.raises(TelegramError) as error:
        client.send_message("123", "hello")
    assert error.value.retry_after is None


def test_updates_request_includes_offset_and_only_messages():
    updates = [{"update_id": 456, "message": {"text": "/start"}}]

    def handler(request):
        assert json.loads(request.content) == {
            "offset": 456,
            "timeout": 30,
            "limit": 100,
            "allowed_updates": ["message"],
        }
        assert request.extensions["timeout"]["read"] == 40
        return httpx.Response(200, json={"ok": True, "result": updates})

    assert client_for(handler).get_updates(456, 30) == updates


@pytest.mark.parametrize("result", [{}, [None], [{"message": {}}], [{"update_id": True}]])
def test_updates_shape_is_validated(result):
    client = client_for(lambda request: httpx.Response(200, json={"ok": True, "result": result}))
    with pytest.raises(TelegramError):
        client.get_updates(0)


def test_get_me_validates_bot_identity():
    identity = {"id": 123456, "is_bot": True, "username": "tower_bot"}
    client = client_for(lambda request: httpx.Response(200, json={"ok": True, "result": identity}))
    assert client.get_me() == identity


@pytest.mark.parametrize(
    "token", ["", "bad token", "123:secret/path", "123:secret?query=1", "123:secret\n"]
)
def test_token_cannot_change_request_path(token):
    with pytest.raises(ValueError):
        TelegramClient(token)


def test_message_limit_counts_astral_characters():
    client = client_for(lambda request: pytest.fail("Invalid messages must not be sent"))
    with pytest.raises(ValueError):
        client.send_message("123", "😀" * 2049)


@pytest.mark.parametrize("offset,timeout", [(-1, 0), (True, 0), (0, 51), (0, -1)])
def test_invalid_update_parameters_do_not_send(offset, timeout):
    client = client_for(lambda request: pytest.fail("Invalid requests must not be sent"))
    with pytest.raises(ValueError):
        client.get_updates(offset, timeout)
