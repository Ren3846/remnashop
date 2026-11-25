import pytest

from traffic_bot.bot import ConfigError, Landing, build_url, load_config, pick_tag

LANDING = Landing(
    tag="vpn1", text="t", button="b", style="primary", extra_tags=frozenset({"vpn1_a"})
)


def test_url_is_an_ad_deeplink_of_the_main_bot() -> None:
    assert build_url("viivpn_bot", "vpn1") == "https://t.me/viivpn_bot?start=ad_vpn1"


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("/start", "vpn1"),
        ("привет", "vpn1"),
        (None, "vpn1"),
        ("/start vpn1_a", "vpn1_a"),
        ("/start unknown", "vpn1"),
        ("hello vpn1_a", "vpn1"),
    ],
)
def test_tag_comes_from_the_bot_unless_an_allowed_one_is_passed(text, tag) -> None:
    assert pick_tag(LANDING, text) == tag


def test_config_uses_defaults_and_per_bot_overrides() -> None:
    target, bots = load_config(
        {
            "target": "@viivpn_bot",
            "defaults": {"text": "common"},
            "bots": [
                {"token": "1:a", "tag": "vpn1"},
                {"token": "2:b", "tag": "ads1", "text": "own", "button": "Go"},
            ],
        }
    )
    assert target == "viivpn_bot"
    assert bots["1:a"].text == "common" and bots["1:a"].button
    assert (bots["2:b"].text, bots["2:b"].button) == ("own", "Go")
    assert bots["1:a"].style == "primary"


def test_style_can_be_switched_off_and_is_validated() -> None:
    raw = {"target": "viivpn_bot", "bots": [{"token": "1:a", "tag": "x", "text": "t"}]}
    raw["bots"][0]["style"] = "default"
    assert load_config(raw)[1]["1:a"].style is None
    raw["bots"][0]["style"] = "blue"
    with pytest.raises(ConfigError):
        load_config(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "viivpn_bot", "bots": []},
        {"target": "", "bots": [{"token": "1:a", "tag": "x", "text": "t"}]},
        {"target": "viivpn_bot", "bots": [{"token": "1:a", "tag": "бот 1", "text": "t"}]},
        {"target": "viivpn_bot", "bots": [{"token": "1:a", "tag": "x"}]},
        {
            "target": "viivpn_bot",
            "bots": [{"token": "1:a", "tag": "x", "text": "t"}] * 2,
        },
    ],
)
def test_bad_config_is_rejected(raw) -> None:
    with pytest.raises(ConfigError):
        load_config(raw)


async def test_start_is_answered_with_one_button_to_the_main_bot(monkeypatch) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message, Update

    from traffic_bot.bot import build_dispatcher

    sent: list[SendMessage] = []

    async def fake_call(self, method, request_timeout=None):  # noqa: ANN001, ANN202
        sent.append(method)

    monkeypatch.setattr(Bot, "__call__", fake_call)
    bot = Bot("42:token")
    dp = build_dispatcher("viivpn_bot", {42: LANDING})
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=7, type="private"),
        text="/start",
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))

    (call,) = sent
    rows = call.reply_markup.inline_keyboard
    assert call.text == "t" and call.chat_id == 7
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].url == "https://t.me/viivpn_bot?start=ad_vpn1"
    assert rows[0][0].style == "primary"
