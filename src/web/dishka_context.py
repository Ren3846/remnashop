from typing import Any, AsyncIterator

from dishka import AsyncContainer, Scope
from dishka.integrations.aiogram import AiogramMiddlewareData
from fastapi import Request


def fastapi_request_context() -> dict[Any, Any]:
    return {AiogramMiddlewareData: {}}


async def with_request_container(request: Request) -> AsyncIterator[AsyncContainer]:
    container: AsyncContainer = request.app.state.dishka_container
    async with container(fastapi_request_context(), scope=Scope.REQUEST) as request_container:
        yield request_container
