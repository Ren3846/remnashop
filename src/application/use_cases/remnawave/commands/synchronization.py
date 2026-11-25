from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.application.common import Cryptographer, Interactor, Remnawave
from src.application.common.dao import PlanDao, SubscriptionDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import PlanSnapshotDto, RemnaSubscriptionDto, SubscriptionDto, UserDto
from src.core.config import AppConfig
from src.core.constants import IMPORTED_TAG
from src.core.enums import Role, SubscriptionStatus
from src.core.types import RemnaUserDto
from src.core.utils.converters import limits_to_plan_type
from src.core.utils.time import datetime_now


@dataclass(frozen=True)
class SyncRemnaUserDto:
    remna_user: RemnaUserDto
    creating: bool


class SyncRemnaUser(Interactor[SyncRemnaUserDto, bool]):
    required_permission = Permission.USER_SYNC

    def __init__(
        self,
        uow: UnitOfWork,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        plan_dao: PlanDao,
        config: AppConfig,
        remnawave: Remnawave,
        cryptographer: Cryptographer,
    ) -> None:
        self.uow = uow
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.plan_dao = plan_dao
        self.config = config
        self.remnawave = remnawave
        self.cryptographer = cryptographer

    async def _execute(self, actor: UserDto, data: SyncRemnaUserDto) -> bool:
        remna_user = data.remna_user

        async with self.uow:
            user = await self.user_dao.get_by_remna_id(remna_user.id)

            if not user and remna_user.telegram_id:
                user = await self.user_dao.get_by_telegram_id(remna_user.telegram_id)
                if user and await self._is_bound_to_another_panel_user(user.id, remna_user.id):
                    logger.info(
                        f"RemnaUser '{remna_user.id}' shares a Telegram id with {user.log}, "
                        f"whose subscription belongs to another panel user: sync skipped"
                    )
                    return False

            if not user and data.creating:
                logger.debug(f"User '{remna_user.id}' not found in bot, creating new user")

                async def persist(referral_code: str) -> UserDto:
                    return await self.user_dao.create(
                        self._create_user_dto(data.remna_user, referral_code)
                    )

                user = await self.uow.persist_with_unique_code(
                    generate=lambda: self.cryptographer.generate_unique_code(
                        self.user_dao.get_by_referral_code
                    ),
                    persist=persist,
                    column="referral_code",
                )

            if not user:
                logger.warning(f"Sync failed: user '{remna_user.id}' could not be found or created")
                return False

            subscription = await self.subscription_dao.get_current(user.id)
            remna_subscription = RemnaSubscriptionDto.from_remna_user(remna_user)

            if not subscription:
                logger.info(
                    f"No subscription found for user '{user.remna_name}', importing from panel"
                )
                await self._import_subscription(user.id, remna_subscription)
                await self.uow.commit()
                logger.info(f"Sync completed for user '{remna_user.telegram_id}'")
                return False
            else:
                logger.info(f"Synchronizing existing subscription for user {user.log}")
                changed = await self._update_subscription(subscription, remna_subscription)
                await self.uow.commit()
                logger.info(f"Sync completed for user '{remna_user.telegram_id}'")
                return changed

    async def _is_bound_to_another_panel_user(self, user_id: int, remna_id: int) -> bool:
        # One person can own two panel users (a bot purchase and a landing purchase).
        # A Telegram id alone must not let one of them overwrite the other's subscription.
        # A bound panel user that no longer exists does not count: that is a re-creation.
        bound = await self.subscription_dao.get_current(user_id)
        if not bound or bound.user_remna_id in (None, remna_id):
            return False
        return await self.remnawave.get_user_by_id(bound.user_remna_id) is not None

    def _create_user_dto(self, data: RemnaUserDto, referral_code: str) -> UserDto:
        return UserDto(
            telegram_id=data.telegram_id,
            referral_code=referral_code,
            name=str(data.telegram_id) if data.telegram_id else str(data.id),
            role=Role.USER,
            language=self.config.default_locale,
        )

    async def _matching_plan_snapshot(
        self,
        remna_subscription: RemnaSubscriptionDto,
    ) -> Optional[PlanSnapshotDto]:
        # A subscription bought on the landing is created straight in the panel from one of
        # our plans. Recognise it by its limits and squads so the bot shows the plan's name
        # and can renew it; anything else stays a nameless import.
        matches = [
            plan
            for plan in await self.plan_dao.get_all()
            if not plan.is_trial
            and plan.durations
            and plan.traffic_limit == remna_subscription.traffic_limit
            and plan.device_limit == remna_subscription.device_limit
            and set(plan.internal_squads) == set(remna_subscription.internal_squads)
        ]
        if len(matches) != 1:
            return None

        plan = matches[0]
        days_left = 0
        if remna_subscription.expire_at:
            days_left = max((remna_subscription.expire_at - datetime_now()).days, 0)
        durations = sorted(duration.days for duration in plan.durations)
        duration = next((days for days in durations if days >= days_left), durations[-1])
        return PlanSnapshotDto.from_plan(plan, duration)

    async def _import_subscription(
        self,
        user_id: int,
        remna_subscription: RemnaSubscriptionDto,
    ) -> None:
        plan = await self._matching_plan_snapshot(remna_subscription) or PlanSnapshotDto(
            id=-1,
            name=IMPORTED_TAG,
            tag=remna_subscription.tag,
            type=limits_to_plan_type(
                remna_subscription.traffic_limit,
                remna_subscription.device_limit,
            ),
            traffic_limit=remna_subscription.traffic_limit,
            device_limit=remna_subscription.device_limit,
            duration=0,
            traffic_limit_strategy=remna_subscription.traffic_limit_strategy,
            internal_squads=remna_subscription.internal_squads,
            external_squad=remna_subscription.external_squad,
        )

        expired = remna_subscription.expire_at and remna_subscription.expire_at < datetime_now()
        status = (
            SubscriptionStatus.EXPIRED if expired else SubscriptionStatus(remna_subscription.status)
        )

        subscription = SubscriptionDto(
            user_remna_id=remna_subscription.remna_id,
            status=status,
            traffic_limit=plan.traffic_limit,
            device_limit=plan.device_limit,
            traffic_limit_strategy=plan.traffic_limit_strategy,
            tag=plan.tag,
            internal_squads=remna_subscription.internal_squads,
            external_squad=remna_subscription.external_squad,
            expire_at=remna_subscription.expire_at,
            url=remna_subscription.url,
            plan_snapshot=plan,
        )

        await self.subscription_dao.create(subscription, user_id)

    async def _update_subscription(
        self,
        target: SubscriptionDto,
        source: RemnaSubscriptionDto,
    ) -> bool:
        subscription = self.remnawave.apply_sync(target, source)
        await self.subscription_dao.update(subscription)
        return bool(subscription.changed_data)


class SyncAllUsersFromBot(Interactor[None, dict[str, int]]):
    required_permission = None

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

    async def _execute(self, actor: UserDto, data: None) -> dict[str, int]:
        bot_users = await self._fetch_all_bot_users()

        logger.info(f"Total users in bot for reverse sync: '{len(bot_users)}'")

        updated = 0
        recreated = 0
        skipped = 0
        errors = 0

        for user in bot_users:
            try:
                subscription = await self.subscription_dao.get_current(user.id)

                if not subscription:
                    skipped += 1
                    continue

                if subscription.user_remna_id is None:
                    # Unlinked after the uuid -> id migration: never recreate or re-bind.
                    logger.warning(f"Skipping unlinked subscription '{subscription.id}'")
                    skipped += 1
                    continue

                remna_user = await self.remnawave.get_user_by_id(subscription.user_remna_id)

                if remna_user:
                    updated_user = await self.remnawave.update_user(
                        user=user,
                        remna_id=subscription.remna_id,
                        subscription=subscription,
                    )
                    if updated_user.subscription_url != subscription.url:
                        subscription.url = updated_user.subscription_url
                        async with self.uow:
                            await self.subscription_dao.update(subscription)
                            await self.uow.commit()
                    updated += 1
                else:
                    created_user = await self.remnawave.create_user(
                        user=user,
                        subscription=subscription,
                    )
                    await self.sync_remna_user.system(
                        SyncRemnaUserDto(created_user, creating=False)
                    )
                    recreated += 1

            except Exception as exception:
                logger.exception(f"Error reverse-syncing bot user {user.log}: {exception}")
                errors += 1

        result = {
            "total_bot_users": len(bot_users),
            "updated": updated,
            "recreated": recreated,
            "skipped_no_subscription": skipped,
            "errors": errors,
        }

        logger.info(f"Reverse sync (bot → panel) summary: '{result}'")
        return result

    async def _fetch_all_bot_users(self) -> list[UserDto]:
        all_users: list[UserDto] = []
        limit = 500
        offset = 0

        while True:
            batch = await self.user_dao.get_all(limit=limit, offset=offset)
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)

        return all_users


class SyncAllUsersFromPanel(Interactor[None, dict[str, int]]):
    required_permission = None

    def __init__(
        self,
        remnawave: Remnawave,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        sync_remna_user: SyncRemnaUser,
    ) -> None:
        self.remnawave = remnawave
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.sync_remna_user = sync_remna_user

    async def _execute(self, actor: UserDto, data: None) -> dict[str, int]:
        panel_users = await self._fetch_all_panel_users()
        bot_users = await self._fetch_all_bot_users()
        bot_users_map = {user.telegram_id: user for user in bot_users}

        logger.info(f"Total users in panel: '{len(panel_users)}'")
        logger.info(f"Total users in bot: '{len(bot_users)}'")

        added_users = 0
        added_subscription = 0
        updated = 0
        errors = 0

        for remna_user in panel_users:
            try:
                if remna_user.telegram_id:
                    user = bot_users_map.get(remna_user.telegram_id)
                else:
                    user = await self.user_dao.get_by_remna_id(remna_user.id)

                if not user:
                    await self.sync_remna_user.system(SyncRemnaUserDto(remna_user, True))
                    added_users += 1
                else:
                    current_subscription = await self.subscription_dao.get_current(user.id)
                    if not current_subscription:
                        await self.sync_remna_user.system(SyncRemnaUserDto(remna_user, True))
                        added_subscription += 1
                    else:
                        changed = await self.sync_remna_user.system(
                            SyncRemnaUserDto(remna_user, True)
                        )
                        if changed:
                            updated += 1

            except Exception as exception:
                logger.exception(
                    f"Error syncing RemnaUser '{remna_user.id}' exception: {exception}"
                )
                errors += 1

        result = {
            "total_panel_users": len(panel_users),
            "total_bot_users": len(bot_users),
            "added_users": added_users,
            "added_subscription": added_subscription,
            "updated": updated,
            "errors": errors,
        }

        logger.info(f"Sync users summary: '{result}'")
        return result

    async def _fetch_all_panel_users(self) -> list[RemnaUserDto]:
        all_users: list[RemnaUserDto] = []
        limit = 50
        offset = 0

        while True:
            batch = await self.remnawave.get_all_users(limit=limit, offset=offset)
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)

        return all_users

    async def _fetch_all_bot_users(self) -> list[UserDto]:
        all_users: list[UserDto] = []
        limit = 500
        offset = 0

        while True:
            batch = await self.user_dao.get_all(limit=limit, offset=offset)
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)

        return all_users
