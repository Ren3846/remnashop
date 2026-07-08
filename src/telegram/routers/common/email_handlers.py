from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, status
from aiogram.types import Message
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.input import MessageInput
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.dto import TelegramUserDto
from src.application.use_cases.auth.commands.email import (
    ConfirmEmailVerification,
    ConfirmEmailVerificationDto,
    RequestEmailVerification,
    RequestEmailVerificationDto,
)
from src.application.use_cases.gateways.commands.payment import (
    CompletePaidPurchase,
    CompletePaidPurchaseDto,
)
from src.application.use_cases.user import LinkUserEmail, LinkUserEmailDto
from src.core.constants import USER_KEY
from src.core.enums import Role
from src.core.exceptions import EmailAlreadyUsedError, EmailDeliveryDisabledError
from src.core.utils.validators import is_valid_email
from src.telegram.states import MainMenu, Subscription

EMAIL_AFTER_KEY = "email_after"
EMAIL_PENDING_PAYMENT_KEY = "pending_payment_id"
EMAIL_PENDING_ADDRESS_KEY = "pending_email_address"


async def redirect_if_email_required(
    dialog_manager: DialogManager,
    user: TelegramUserDto,
    *,
    email_after: str,
    email_state: Any = Subscription.EMAIL,
) -> bool:
    if user.email and user.is_email_verified:
        return False

    dialog_manager.dialog_data[EMAIL_AFTER_KEY] = email_after
    await dialog_manager.switch_to(email_state)
    return True


@inject
async def on_link_email_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    request_email_verification: RequestEmailVerification,
    link_user_email: LinkUserEmail,
    complete_paid_purchase: CompletePaidPurchase,
    notifier: Notifier,
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    email = (message.text or "").strip()

    if not is_valid_email(email):
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.invalid-email")
        return

    if user.telegram_id is None:
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        return

    try:
        await request_email_verification(
            user,
            RequestEmailVerificationDto(email=email),
        )
    except EmailDeliveryDisabledError:
        await notifier.notify_user(user=user, i18n_key="ntf-user.email-delivery-disabled")
        return
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            await notifier.notify_user(user=user, i18n_key="ntf-user.email-already-used")
            return
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            await notifier.notify_user(user=user, i18n_key="ntf-user.email-code-rate-limit")
            return
        logger.exception(f"{user.log} Failed to request verification code for '{email}'")
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        raise
    except Exception:
        logger.exception(f"{user.log} Failed to request verification code for '{email}'")
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        raise

    dialog_manager.dialog_data[EMAIL_PENDING_ADDRESS_KEY] = email
    await notifier.notify_user(user=user, i18n_key="ntf-user.email-code-sent")

    current_state = dialog_manager.current_context().state
    if current_state == MainMenu.LINK_EMAIL:
        await dialog_manager.switch_to(MainMenu.LINK_EMAIL_CONFIRM)
    else:
        await dialog_manager.switch_to(Subscription.LINK_EMAIL_CONFIRM)


@inject
async def on_confirm_email_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    confirm_email_verification: ConfirmEmailVerification,
    link_user_email: LinkUserEmail,
    complete_paid_purchase: CompletePaidPurchase,
    notifier: Notifier,
) -> None:
    user = dialog_manager.middleware_data[USER_KEY]
    code = (message.text or "").strip()

    if not code.isdigit():
        await notifier.notify_user(user=user, i18n_key="ntf-user.email-code-invalid")
        return

    try:
        verified = await confirm_email_verification(
            user,
            ConfirmEmailVerificationDto(code=code),
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_410_GONE:
            await notifier.notify_user(user=user, i18n_key="ntf-user.email-code-expired")
            return
        await notifier.notify_user(user=user, i18n_key="ntf-user.email-code-invalid")
        return
    except Exception:
        logger.exception(f"{user.log} Failed to confirm email code")
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        raise

    if user.telegram_id is None:
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        return

    try:
        updated_user = await link_user_email(
            user,
            LinkUserEmailDto(telegram_id=user.telegram_id, email=verified.email),
        )
    except EmailAlreadyUsedError:
        await notifier.notify_user(user=user, i18n_key="ntf-user.email-already-used")
        return
    except Exception:
        logger.exception(f"{user.log} Failed to sync linked email '{verified.email}'")
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        raise

    user.email = updated_user.email
    user.is_email_verified = True
    user.pending_email = None
    user.email_verification_code_hash = None
    user.email_verification_expires_at = None
    dialog_manager.dialog_data.pop(EMAIL_PENDING_ADDRESS_KEY, None)

    start_data = cast(dict[str, Any], dialog_manager.start_data or {})
    payment_id_str = (
        start_data.get("payment_id")
        or dialog_manager.dialog_data.pop(EMAIL_PENDING_PAYMENT_KEY, None)
    )

    if payment_id_str:
        try:
            await complete_paid_purchase.system(
                CompletePaidPurchaseDto(payment_id=UUID(str(payment_id_str)))
            )
        except Exception:
            logger.exception(f"{user.log} Failed to activate subscription after email input")
            await notifier.notify_user(user, i18n_key="ntf-subscription.payment-creation-failed")
            raise
        return

    email_after = dialog_manager.dialog_data.pop(EMAIL_AFTER_KEY, None)
    await notifier.notify_user(user=user, i18n_key="ntf-user.email-linked")

    if email_after == "menu":
        await dialog_manager.switch_to(MainMenu.MAIN)
    elif email_after in {"subscription", "trial", "landing"}:
        await dialog_manager.switch_to(Subscription.MAIN)
    else:
        await dialog_manager.switch_to(MainMenu.MAIN)


def should_prompt_email_on_start(user: TelegramUserDto) -> bool:
    return (not user.email or not user.is_email_verified) and user.role != Role.OWNER
