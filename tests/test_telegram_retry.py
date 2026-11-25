import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiohttp import ClientConnectorError

from src.infrastructure.di.providers.bot import RetryConnectMiddleware

METHOD = SimpleNamespace(__api_method__="editMessageMedia")


def connect_error() -> ClientConnectorError:
    key = SimpleNamespace(host="api.telegram.org", port=443, ssl=True, is_ssl=True)
    return ClientConnectorError(key, OSError(104, "Connection reset by peer"))


def run(failures: list[BaseException]):
    calls = []

    async def make_request(bot, method):
        calls.append(method)
        if failures:
            cause = failures.pop(0)
            raise TelegramNetworkError(method=method, message="boom") from cause
        return "ok"

    async def go():
        return await RetryConnectMiddleware()(make_request, None, METHOD)

    return go, calls


def test_failed_connect_is_retried_once(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())
    go, calls = run([connect_error()])
    assert asyncio.run(go()) == "ok"
    assert len(calls) == 2


def test_second_failed_connect_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", lambda _: _noop())
    go, calls = run([connect_error(), connect_error()])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(go())
    assert len(calls) == 2


def test_error_after_the_request_was_sent_is_not_retried() -> None:
    # The request may have reached Telegram: sending it again could duplicate a message.
    go, calls = run([TimeoutError("read timeout")])
    with pytest.raises(TelegramNetworkError):
        asyncio.run(go())
    assert len(calls) == 1


async def _noop() -> None:
    return None
