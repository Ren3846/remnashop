from dataclasses import dataclass
from typing import Optional

from loguru import logger
from remnapy.models import UserResponseDto

from src.application.common import Interactor, Remnawave
from src.application.common.dao import SubscriptionDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import UserDto
from src.application.use_cases.remnawave.commands.synchronization import (
    SyncRemnaUser,
    SyncRemnaUserDto,
)
from src.core.exceptions import EmailAlreadyUsedError
from src.core.utils.validators import is_valid_email


@dataclass(frozen=True)
class LinkUserEmailDto:
    telegram_id: int
    email: str


class LinkUserEmail(Interactor[LinkUserEmailDto, UserDto]):
    """Bind email to bot user and sync identity with Remnawave (landing ↔ bot)."""

    required_permission = Permission.PUBLIC

    def __init__(
        self,
        uow: UnitOfWork,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        remnawave: Remnawave,
        sync_remna_user: SyncRemnaUser,
    ) -> None:
        self.uow = uow
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.remnawave = remnawave
        self.sync_remna_user = sync_remna_user

    async def _execute(self, actor: UserDto, data: LinkUserEmailDto) -> UserDto:
        email = data.email.strip()
        if not is_valid_email(email):
            raise ValueError(f"Invalid email format: {email!r}")

        existing = await self.user_dao.get_by_email(email)

        async with self.uow:
            user = await self.user_dao.get_by_telegram_id(data.telegram_id)
            if not user:
                raise ValueError(f"User '{data.telegram_id}' not found")

            if existing and existing.id != user.id:
                raise EmailAlreadyUsedError(email)

            user.email = email
            await self.user_dao.update(user)
            await self._sync_remna_identity(user, email)
            await self.uow.commit()

        logger.info(f"{actor.log} Email linked to '{email}' and synced with Remnawave")
        return user

    async def _sync_remna_identity(self, user: UserDto, email: str) -> None:
        bot_subscription = await self.subscription_dao.get_current(user.id)
        remna_by_email = await self.remnawave.get_users_by_email(email)
        remna_by_tg: list[UserResponseDto] = []
        if user.telegram_id is not None:
            remna_by_tg = await self.remnawave.get_users_by_telegram_id(user.telegram_id)

        bound_user: Optional[UserResponseDto] = None
        if bot_subscription:
            bound_user = await self.remnawave.get_user_by_id(bot_subscription.user_remna_id)

        # A purchase made on the landing lives in its own panel user, found by the email
        # that was just verified with a code.
        landing_user = self._pick_remna_user(
            [u for u in remna_by_email if bound_user is None or u.id != bound_user.id]
        )
        if landing_user and (
            landing_user.telegram_id is not None
            and user.telegram_id is not None
            and landing_user.telegram_id != user.telegram_id
        ):
            raise EmailAlreadyUsedError(email)

        if bound_user:
            if not landing_user or not self._supersedes(landing_user, bound_user):
                await self._ensure_remna_identity(bound_user.id, user.telegram_id, email)
                return

            # The bot already shows an older subscription, but the one bought on the landing
            # runs longer: make it the current one. The old panel user is left untouched and
            # simply runs out.
            await self.user_dao.clear_current_subscription(user.id)
            logger.info(
                f"{user.log} Landing subscription (panel id {landing_user.id}) replaces "
                f"panel id {bound_user.id} as the current one"
            )

        if landing_user:
            await self._ensure_remna_identity(landing_user.id, user.telegram_id, email)
            await self.sync_remna_user.system(SyncRemnaUserDto(landing_user, creating=False))
            return

        if remna_by_tg:
            await self._ensure_remna_identity(remna_by_tg[0].id, user.telegram_id, email)

    async def _ensure_remna_identity(
        self,
        remna_id: int,
        telegram_id: Optional[int],
        email: str,
    ) -> None:
        await self.remnawave.link_identity(
            remna_id,
            telegram_id=telegram_id,
            email=email,
        )

    @staticmethod
    def _supersedes(candidate: UserResponseDto, current: UserResponseDto) -> bool:
        if str(candidate.status).upper() != "ACTIVE":
            return False
        if str(current.status).upper() != "ACTIVE":
            return True
        return candidate.expire_at > current.expire_at

    @staticmethod
    def _pick_remna_user(users: list[UserResponseDto]) -> Optional[UserResponseDto]:
        if not users:
            return None
        if len(users) == 1:
            return users[0]

        def sort_key(user: UserResponseDto) -> tuple[int, float]:
            expire_ts = user.expire_at.timestamp() if user.expire_at else 0.0
            status_rank = 1 if str(user.status).upper() == "ACTIVE" else 0
            return (status_rank, expire_ts)

        return max(users, key=sort_key)
