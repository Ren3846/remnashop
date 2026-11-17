from uuid import UUID

from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger

from src.application.services import LandingPaymentService
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.application.use_cases.misc.commands.maintenance import CancelOldTransactions
from src.core.enums import PaymentGatewayType, TransactionStatus
from src.infrastructure.taskiq.broker import broker


@broker.task()
@inject(patch_module=True)
async def handle_payment_transaction_task(
    payment_id: UUID,
    payment_status: TransactionStatus,
    gateway_type: PaymentGatewayType,
    process_payment: FromDishka[ProcessPayment],
    landing_payment: FromDishka[LandingPaymentService],
) -> None:
    # Purchases made on the landing have no bot transaction: the order lives in Redis
    # under the Lava contract id. Fulfil it here, otherwise the customer pays and gets nothing.
    if (
        gateway_type == PaymentGatewayType.LAVA
        and payment_status == TransactionStatus.COMPLETED
        and await landing_payment.try_fulfill(payment_id)
    ):
        logger.info(f"Landing payment '{payment_id}' fulfilled")
        return

    await process_payment.system(
        ProcessPaymentDto(
            payment_id=payment_id,
            new_transaction_status=payment_status,
            gateway_type=gateway_type,
        )
    )


@broker.task(schedule=[{"cron": "*/30 * * * *"}])
@inject(patch_module=True)
async def cancel_old_transactions_task(
    cancel_old_transactions: FromDishka[CancelOldTransactions],
) -> None:
    await cancel_old_transactions.system()
