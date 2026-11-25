import asyncio
from collections.abc import AsyncIterable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram_dialog import BgManagerFactory
from aiohttp import ClientConnectorError
from dishka import Provider, Scope, from_context, provide
from loguru import logger

from src.core.config import AppConfig


def _normalize_proxy_url(url: str) -> str:
    # aiohttp_socks doesn't recognize the socks5h/socks4a schemes; aiogram always
    # resolves DNS remotely (rdns=True), so they're equivalent to socks5/socks4.
    if url.startswith("socks5h://"):
        return url.replace("socks5h://", "socks5://", 1)
    if url.startswith("socks4a://"):
        return url.replace("socks4a://", "socks4://", 1)
    return url


class RetryConnectMiddleware(BaseRequestMiddleware):
    """Retry a request once when the connection to Telegram could not be opened.

    Only a failed connect is retried: nothing was sent, so nothing can be duplicated.
    """

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            return await make_request(bot, method)
        except TelegramNetworkError as e:
            if not isinstance(e.__cause__, ClientConnectorError):
                raise
            logger.warning(f"Could not connect to Telegram, retrying once: {e}")
            await asyncio.sleep(0.5)
            return await make_request(bot, method)


class BotProvider(Provider):
    scope = Scope.APP

    bg_manager_factory = from_context(provides=BgManagerFactory)

    @provide
    async def get_bot(self, config: AppConfig) -> AsyncIterable[Bot]:
        logger.debug("Initializing Bot instance")

        session = None
        if config.bot.proxy_url:
            logger.info("Using SOCKS5 proxy for Telegram")
            proxy = _normalize_proxy_url(config.bot.proxy_url.get_secret_value())
            session = AiohttpSession(proxy=proxy)

        async with Bot(
            token=config.bot.token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=session,
        ) as bot:
            bot.session.middleware(RetryConnectMiddleware())
            yield bot
