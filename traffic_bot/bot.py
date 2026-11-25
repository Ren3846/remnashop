"""Traffic (pre-landing) bots for ViVPN.

Each bot answers anything with one short text and ONE button that opens the main bot through
an advertising deep link (``https://t.me/<main>?start=ad_<tag>``), so every traffic bot is
counted separately in the main bot: dashboard -> Advertising.

Standalone on purpose: needs only aiogram, no database, nothing from ``src``.
Config is a JSON file (path in TRAFFIC_BOTS_CONFIG, default ./bots.json), see bots.example.json.
"""

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger("traffic_bot")

# Telegram allows 64 chars of [A-Za-z0-9_-] in a start parameter; "ad_" takes three of them.
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,61}$")
DEFAULT_BUTTON = "🚀 Подключить VPN"
# Bot API 9.4 button colours: primary = blue, success = green, danger = red.
BUTTON_STYLES = {"primary", "success", "danger"}
DEFAULT_STYLE = "primary"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Landing:
    tag: str
    text: str
    button: str
    style: Optional[str]
    # Extra tags this bot accepts from its own link (t.me/<traffic bot>?start=<tag>),
    # for several creatives leading to one traffic bot.
    extra_tags: frozenset[str]


def build_url(target: str, tag: str) -> str:
    return f"https://t.me/{target}?start=ad_{tag}"


def pick_tag(landing: Landing, message_text: Optional[str]) -> str:
    parts = (message_text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0].startswith("/start"):
        payload = parts[1].strip()
        if payload in landing.extra_tags:
            return payload
    return landing.tag


def _check_tag(tag: Any) -> str:
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise ConfigError(f"Bad tag {tag!r}: latin letters, digits, '_' and '-' only, up to 61")
    return tag


def _check_style(style: Any) -> Optional[str]:
    """None or "default" keeps Telegram's ordinary grey button."""
    if style in (None, "", "default"):
        return None
    if style not in BUTTON_STYLES:
        raise ConfigError(f"Bad style {style!r}: one of {sorted(BUTTON_STYLES)} or 'default'")
    return str(style)


def load_config(raw: dict[str, Any]) -> tuple[str, dict[str, Landing]]:
    """Returns (main bot username, {token: landing})."""
    target = str(raw.get("target", "")).lstrip("@")
    if not re.match(r"^[A-Za-z0-9_]{5,32}$", target):
        raise ConfigError("'target' must be the main bot username")

    defaults = raw.get("defaults", {})
    bots = raw.get("bots")
    if not isinstance(bots, list) or not bots:
        raise ConfigError("'bots' must be a non-empty list")

    result: dict[str, Landing] = {}
    for item in bots:
        token = item.get("token")
        if not isinstance(token, str) or ":" not in token:
            raise ConfigError("Every bot needs a 'token'")
        if token in result:
            raise ConfigError("The same token is listed twice")
        text = item.get("text") or defaults.get("text")
        if not text:
            raise ConfigError(f"Bot with tag {item.get('tag')!r} has no 'text'")
        result[token] = Landing(
            tag=_check_tag(item.get("tag")),
            text=text,
            button=item.get("button") or defaults.get("button") or DEFAULT_BUTTON,
            style=_check_style(item.get("style", defaults.get("style", DEFAULT_STYLE))),
            extra_tags=frozenset(_check_tag(t) for t in item.get("extra_tags", [])),
        )
    return target, result


def build_dispatcher(target: str, landings: dict[int, Landing]) -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def show_landing(message: Message, bot: Bot) -> None:
        landing = landings[bot.id]
        tag = pick_tag(landing, message.text)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=landing.button,
                        url=build_url(target, tag),
                        style=landing.style,
                    )
                ]
            ]
        )
        await message.answer(landing.text, reply_markup=keyboard, disable_web_page_preview=True)
        log.info("shown tag=%s bot=%s user=%s", tag, bot.id, message.chat.id)

    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # aiogram logs request URLs (with the token) only at DEBUG; keep it above that.
    logging.getLogger("aiogram").setLevel(logging.WARNING)

    path = Path(os.environ.get("TRAFFIC_BOTS_CONFIG", Path(__file__).with_name("bots.json")))
    try:
        target, by_token = load_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ConfigError) as error:
        sys.exit(f"traffic_bot config error: {error}")

    props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bots = [Bot(token, default=props) for token in by_token]
    landings = {bot.id: by_token[bot.token] for bot in bots}

    for bot in bots:
        me = await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("@%s -> %s", me.username, build_url(target, landings[bot.id].tag))

    await build_dispatcher(target, landings).start_polling(*bots, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
