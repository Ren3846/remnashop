import asyncio
from typing import Any
from uuid import UUID

from dishka import AsyncContainer, Scope
from dishka.integrations.aiogram import AiogramMiddlewareData
from loguru import logger

from src.application.common import EventPublisher
from src.application.events import ErrorEvent
from src.application.services.landing_payment import LandingPaymentService
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.core.config import AppConfig
from src.core.enums import TransactionStatus

_payment_tasks: set[asyncio.Task[None]] = set()


def _background_request_context() -> dict[Any, Any]:
    # AiogramMiddlewareData is required by I18nAiogramProvider outside telegram updates.
    return {AiogramMiddlewareData: {}}


async def schedule_payment_processing(
    container: AsyncContainer,
    config: AppConfig,
    payment_id: UUID,
    payment_status: TransactionStatus,
) -> None:
    task = asyncio.create_task(
        _process_payment(container, config, payment_id, payment_status),
        name=f"process-payment-{payment_id}",
    )
    _payment_tasks.add(task)
    task.add_done_callback(_payment_tasks.discard)
    logger.debug(f"Scheduled payment processing for '{payment_id}'")


async def _process_payment(
    container: AsyncContainer,
    config: AppConfig,
    payment_id: UUID,
    payment_status: TransactionStatus,
) -> None:
    request_context = _background_request_context()

    try:
        async with container(request_context, scope=Scope.REQUEST) as request_container:
            if payment_status == TransactionStatus.COMPLETED:
                landing_payment = await request_container.get(LandingPaymentService)
                if await landing_payment.try_fulfill(payment_id):
                    logger.info(f"Landing payment '{payment_id}' fulfilled")
                    return

            process_payment = await request_container.get(ProcessPayment)
            await process_payment.system(ProcessPaymentDto(payment_id, payment_status))
    except Exception as e:
        logger.exception(f"Failed to process payment '{payment_id}' in background")
        try:
            async with container(request_context, scope=Scope.REQUEST) as request_container:
                event_publisher = await request_container.get(EventPublisher)
                await event_publisher.publish(ErrorEvent(**config.build.data, exception=e))
        except Exception as publish_error:
            logger.exception(
                f"Failed to publish error event for payment '{payment_id}': {publish_error}"
            )
