"""Traffic (pre-landing) bots for ViVPN.

Each bot answers anything with one short text and ONE button that opens the main bot through
an advertising deep link (``https://t.me/<main>?start=ad_<tag>``), so every traffic bot is
counted separately in the main bot: dashboard -> Advertising.

Every visitor is also counted here (sqlite file, TRAFFIC_BOTS_DB): admins listed in the config
get the numbers with /stats. Clicks on a URL button are not reported by Telegram, so the funnel
is "opened the traffic bot" (here) -> "started the main bot" (its Advertising page).

Standalone on purpose: needs only aiogram, nothing from ``src``.
Config is a JSON file (path in TRAFFIC_BOTS_CONFIG, default ./bots.json), see bots.example.json.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
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


class Stats:
    """Who opened which traffic bot under which tag; one row per (bot, tag, user)."""

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "create table if not exists visits ("
            "bot_id integer not null, tag text not null, user_id integer not null, "
            "first_at text not null, last_at text not null, hits integer not null, "
            "primary key (bot_id, tag, user_id))"
        )
        self.db.commit()

    def hit(self, bot_id: int, tag: str, user_id: int, now: Optional[datetime] = None) -> None:
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        self.db.execute(
            "insert into visits values (?, ?, ?, ?, ?, 1) "
            "on conflict (bot_id, tag, user_id) do update set last_at = ?, hits = hits + 1",
            (bot_id, tag, user_id, stamp, stamp, stamp),
        )
        self.db.commit()

    def summary(self, now: Optional[datetime] = None) -> list[tuple[int, str, int, int, int]]:
        """Rows of (bot_id, tag, people, people who first came today (UTC), messages)."""
        today = (now or datetime.now(UTC)).date().isoformat()
        return self.db.execute(
            "select bot_id, tag, count(*), sum(substr(first_at, 1, 10) = ?), sum(hits) "
            "from visits group by bot_id, tag order by bot_id, tag",
            (today,),
        ).fetchall()


def format_stats(rows: list[tuple[int, str, int, int, int]], names: dict[int, str]) -> str:
    if not rows:
        return "Пока никто не заходил."
    lines = ["<b>Бот-прокладки: кто зашёл</b>"]
    for bot_id, tag, people, today, hits in rows:
        lines.append(
            f"\n@{names.get(bot_id, bot_id)} · <code>{tag}</code>\n"
            f"людей: <b>{people}</b> (сегодня новых: {today}), сообщений: {hits}"
        )
    lines.append("\nСколько из них дошло до основного бота — там в «Панель → Реклама».")
    return "\n".join(lines)


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


def load_admins(raw: dict[str, Any]) -> frozenset[int]:
    admins = raw.get("admins", [])
    if not isinstance(admins, list) or not all(type(a) is int for a in admins):
        raise ConfigError("'admins' must be a list of Telegram user ids")
    return frozenset(admins)


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


def build_dispatcher(
    target: str,
    landings: dict[int, Landing],
    stats: Optional[Stats] = None,
    admins: frozenset[int] = frozenset(),
    names: Optional[dict[int, str]] = None,
) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("stats"), F.chat.id.in_(admins))
    async def show_stats(message: Message) -> None:
        rows = stats.summary() if stats else []
        await message.answer(format_stats(rows, names or {}))

    @dp.message()
    async def show_landing(message: Message, bot: Bot) -> None:
        landing = landings[bot.id]
        tag = pick_tag(landing, message.text)
        if stats and message.chat.type == "private" and message.chat.id not in admins:
            try:
                stats.hit(bot.id, tag, message.chat.id)
            except sqlite3.Error:
                log.exception("stats write failed")
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
        raw = json.loads(path.read_text(encoding="utf-8"))
        target, by_token = load_config(raw)
        admins = load_admins(raw)
        stats = Stats(os.environ.get("TRAFFIC_BOTS_DB", str(path.with_name("stats.sqlite"))))
    except (OSError, json.JSONDecodeError, ConfigError, sqlite3.Error) as error:
        sys.exit(f"traffic_bot config error: {error}")

    props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bots = [Bot(token, default=props) for token in by_token]
    landings = {bot.id: by_token[bot.token] for bot in bots}

    names: dict[int, str] = {}
    for bot in bots:
        me = await bot.get_me()
        names[bot.id] = me.username or str(bot.id)
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("@%s -> %s", me.username, build_url(target, landings[bot.id].tag))

    dp = build_dispatcher(target, landings, stats, admins, names)
    await dp.start_polling(*bots, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
