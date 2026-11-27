from typing import Optional
from uuid import UUID

from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger

from src.application.common.dao import UserDao
from src.application.services import LandingPaymentService, PaymentReminderService
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.application.use_cases.misc.commands.maintenance import CancelOldTransactions
from src.application.use_cases.user import LinkUserEmail, LinkUserEmailDto
from src.core.enums import PaymentGatewayType, TransactionStatus
from src.infrastructure.taskiq.broker import broker


@broker.task()
@inject(patch_module=True)
async def handle_payment_transaction_task(
    payment_id: UUID,
    payment_status: TransactionStatus,
    gateway_type: PaymentGatewayType,
    parent_payment_id: Optional[UUID] = None,
    *,
    process_payment: FromDishka[ProcessPayment],
    landing_payment: FromDishka[LandingPaymentService],
) -> None:
    # Purchases made on the landing have no bot transaction: the order lives in Redis
    # under the Lava contract id. Fulfil it here, otherwise the customer pays and gets nothing.
    if gateway_type == PaymentGatewayType.LAVA and payment_status == TransactionStatus.COMPLETED:
        email = await landing_payment.fulfill(payment_id)
        if email:
            logger.info(f"Landing payment '{payment_id}' fulfilled")
            try:
                await attach_landing_purchase_task.kiq(email)  # type: ignore[call-overload]
            except Exception as e:
                logger.error(f"Could not queue attaching the landing purchase for {email}: {e}")
            return

    # A recurring Lava charge belongs to the subscription opened by its first contract.
    is_recurring = (
        gateway_type == PaymentGatewayType.LAVA
        and payment_status == TransactionStatus.COMPLETED
        and parent_payment_id is not None
    )
    if is_recurring and await landing_payment.try_renew(payment_id, parent_payment_id):
        logger.info(f"Landing subscription renewed by recurring payment '{payment_id}'")
        return

    await process_payment.system(
        ProcessPaymentDto(
            payment_id=payment_id,
            new_transaction_status=payment_status,
            gateway_type=gateway_type,
            parent_payment_id=parent_payment_id if is_recurring else None,
        )
    )


@broker.task()
@inject(patch_module=True)
async def attach_landing_purchase_task(
    email: str,
    *,
    user_dao: FromDishka[UserDao],
    link_user_email: FromDishka[LinkUserEmail],
) -> None:
    # The buyer may already use the bot with this email confirmed by a code. Then the
    # purchase shows up there right away, without asking them to link the email again.
    user = await user_dao.get_by_email(email)
    if not user or not user.is_email_verified or user.telegram_id is None:
        return

    try:
        await link_user_email.system(LinkUserEmailDto(telegram_id=user.telegram_id, email=email))
        logger.info(f"Landing purchase for {email} attached to bot user {user.log}")
    except Exception as e:
        # The purchase itself is delivered by email; the buyer can still link it by hand.
        logger.error(f"Landing purchase for {email} was not attached to {user.log}: {e}")


@broker.task(schedule=[{"cron": "*/30 * * * *"}])
@inject(patch_module=True)
async def cancel_old_transactions_task(
    cancel_old_transactions: FromDishka[CancelOldTransactions],
) -> None:
    await cancel_old_transactions.system()


@broker.task()
@inject(patch_module=True)
async def send_payment_link_email_task(
    payment_id: str,
    *,
    payment_reminder: FromDishka[PaymentReminderService],
) -> None:
    await payment_reminder.send_link_email(payment_id)


@broker.task(schedule=[{"cron": "*/2 * * * *"}])
@inject(patch_module=True)
async def remind_unpaid_payments_task(
    payment_reminder: FromDishka[PaymentReminderService],
) -> None:
    sent = await payment_reminder.remind_due()
    if sent:
        logger.info(f"Reminded {sent} user(s) about an unpaid invoice")
