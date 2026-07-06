from typing import Any, Optional
from uuid import UUID

from aiogram import Bot
from aiogram.fsm.state import State
from aiogram_dialog import BgManagerFactory, ShowMode, StartMode
from loguru import logger

from src.application.common import Redirect
from src.core.constants import TARGET_USER_ID
from src.core.enums import PurchaseType
from src.telegram.states import DashboardUser, MainMenu, Subscription


class RedirectImpl(Redirect):
    def __init__(
        self,
        bot: Bot,
        bg_manager_factory: BgManagerFactory,
    ) -> None:
        self.bot = bot
        self.bg_manager_factory = bg_manager_factory

    async def _start_dialog(
        self,
        telegram_id: int,
        state: State,
        action: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        bg_manager = self.bg_manager_factory.bg(
            bot=self.bot,
            user_id=telegram_id,
            chat_id=telegram_id,
        )

        try:
            await bg_manager.start(
                state=state,
                data=data,
                mode=StartMode.RESET_STACK,
                show_mode=ShowMode.DELETE_AND_SEND,
            )
        except Exception as e:
            logger.exception(f"Failed to redirect user '{telegram_id}' to {action}: {e}")
            raise

        logger.info(f"User '{telegram_id}' redirected to {action}")

    async def to_main_menu(self, telegram_id: int) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=MainMenu.MAIN,
            action="main menu",
        )

    async def to_user_editor(self, telegram_id: int, target_user_id: int) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=DashboardUser.MAIN,
            data={TARGET_USER_ID: target_user_id},
            action="user editor",
        )

    async def to_success_trial(self, telegram_id: int) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=Subscription.TRIAL,
            action="success trial",
        )

    async def to_success_payment(self, telegram_id: int, purchase_type: PurchaseType) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=Subscription.SUCCESS,
            data={"purchase_type": purchase_type},
            action="success payment",
        )

    async def to_failed_payment(self, telegram_id: int) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=Subscription.FAILED,
            action="failed payment",
        )

    async def to_post_payment_email(
        self, telegram_id: int, payment_id: UUID, purchase_type: PurchaseType
    ) -> None:
        await self._start_dialog(
            telegram_id=telegram_id,
            state=Subscription.EMAIL,
            data={"payment_id": str(payment_id), "purchase_type": purchase_type.value},
            action=f"post-payment email for payment '{payment_id}'",
        )
