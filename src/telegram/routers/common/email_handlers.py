from typing import Any, Optional, cast
from uuid import UUID

from aiogram.types import Message
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.input import MessageInput
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier
from src.application.use_cases.gateways.commands.payment import (
    CompletePaidPurchase,
    CompletePaidPurchaseDto,
)
from src.application.use_cases.user import LinkUserEmail, LinkUserEmailDto
from src.core.constants import USER_KEY
from src.core.enums import Role
from src.core.exceptions import EmailAlreadyUsedError
from src.core.utils.validators import is_valid_email
from src.telegram.states import MainMenu, Subscription

EMAIL_AFTER_KEY = "email_after"
EMAIL_PENDING_PAYMENT_KEY = "pending_payment_id"


async def redirect_if_email_required(
    dialog_manager: DialogManager,
    user_email: Optional[str],
    *,
    email_after: str,
    email_state: Any = Subscription.EMAIL,
) -> bool:
    if user_email:
        return False

    dialog_manager.dialog_data[EMAIL_AFTER_KEY] = email_after
    await dialog_manager.switch_to(email_state)
    return True


@inject
async def on_link_email_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
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
        updated_user = await link_user_email(
            user,
            LinkUserEmailDto(telegram_id=user.telegram_id, email=email),
        )
    except EmailAlreadyUsedError:
        await notifier.notify_user(user=user, i18n_key="ntf-user.email-already-used")
        return
    except Exception:
        logger.exception(f"{user.log} Failed to link email '{email}'")
        await notifier.notify_user(user=user, i18n_key="ntf-subscription.payment-creation-failed")
        raise

    user.email = updated_user.email

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


def should_prompt_email_on_start(user) -> bool:
    return not user.email and user.role != Role.OWNER
