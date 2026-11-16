import asyncio
from dataclasses import fields, is_dataclass
from datetime import timedelta
from typing import Final, Optional, Union

from loguru import logger
from packaging.version import Version
from remnapy import RemnawaveSDK
from remnapy.enums import UserStatus
from remnapy.exceptions import AuthenticationError, ConflictError, NotFoundError
from remnapy.models import (
    CreateUserRequestDto,
    DeleteUserAllHwidDeviceRequestDto,
    DeleteUserHwidDeviceRequestDto,
    DropByUserIds,
    DropConnectionsRequestDto,
    GetMetadataResponseDto,
    TargetAllNodes,
    UpdateUserRequestDto,
    UserResponseDto,
)
from remnapy.models.hwid import HwidDeviceDto

from src.application.common import Remnawave
from src.application.common.remnawave import T
from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SquadInfoDto,
    SubscriptionDto,
    UserDto,
)
from src.core.constants import REMNAWAVE_MIN_VERSION
from src.core.enums import SubscriptionStatus
from src.core.utils.converters import days_to_datetime, gb_to_bytes
from src.core.utils.time import datetime_now

_STREAM_PAGE_SIZE: Final[int] = 500

# Subscription fields copied from the panel on sync, besides the panel id itself.
_SYNCED_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "expire_at",
    "url",
    "traffic_limit",
    "device_limit",
    "traffic_limit_strategy",
    "tag",
    "internal_squads",
    "external_squad",
)


class RemnawaveImpl(Remnawave):
    def __init__(self, sdk: RemnawaveSDK) -> None:
        self.sdk = sdk

    async def try_connection(self) -> Version:
        for attempt in range(1, 4):
            try:
                metadata = await self.sdk.system.get_metadata()
                break
            except AuthenticationError as e:
                logger.error(f"Authentication failed when connecting to Remnawave panel: '{e}'")
                raise
            except Exception as e:
                if attempt < 3:
                    logger.warning(
                        f"Failed to connect to Remnawave panel (attempt {attempt}/3): '{e}', "
                        f"retrying in 5s..."
                    )
                    await asyncio.sleep(5)
                else:
                    logger.error(f"Failed to connect to Remnawave panel after 3 attempts: '{e}'")
                    raise

        if not isinstance(metadata, GetMetadataResponseDto):
            logger.error(f"Invalid response from Remnawave panel: '{metadata}'")
            raise ValueError(f"Invalid response from Remnawave panel: {metadata}")

        panel_version = Version(metadata.version)
        if panel_version < REMNAWAVE_MIN_VERSION:
            logger.error(
                f"Remnawave panel version '{panel_version}' is not compatible. "
                f"Minimum required version: '{REMNAWAVE_MIN_VERSION}'"
            )
            raise ValueError(
                f"Remnawave panel version '{panel_version}' is not compatible. "
                f"Minimum required version: '{REMNAWAVE_MIN_VERSION}'"
            )

        logger.info(f"Successfully connected to Remnawave panel (version: {panel_version})")
        return panel_version

    async def create_user(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> UserResponseDto:
        request_dto = self._build_create_request(user, plan, subscription)

        try:
            remna_user = await self.sdk.users.create_user(request_dto)
            logger.info(
                f"RemnaUser '{remna_user.username}' created successfully. "
                f"ID: '{remna_user.id}', telegram_id: '{remna_user.telegram_id}'"
            )
            return remna_user
        except ConflictError:
            # Never adopt an existing panel user here: without proven ownership it may
            # belong to someone else. The caller decides what to do.
            logger.warning(f"RemnaUser '{request_dto.username}' already exists in panel")
            raise

    async def update_user(
        self,
        user: UserDto,
        remna_id: int,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> UserResponseDto:
        request_dto = self._build_update_request(user, remna_id, plan, subscription)

        try:
            remna_user = await self.sdk.users.update_user(request_dto)
            logger.info(
                f"RemnaUser '{remna_user.username}' updated successfully. "
                f"ID: '{remna_user.id}', telegram_id: '{remna_user.telegram_id}'"
            )
        except NotFoundError:
            logger.warning(f"RemnaUser '{remna_id}' not found")
            raise

        if reset_traffic:
            await self.reset_traffic(remna_id)

        return remna_user

    async def enable_user(self, remna_id: int) -> None:
        try:
            await self.sdk.users.enable_user(remna_id)
            logger.info(f"RemnaUser '{remna_id}' enabled successfully")
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            raise

    async def disable_user(self, remna_id: int) -> None:
        try:
            await self.sdk.users.disable_user(remna_id)
            logger.info(f"RemnaUser '{remna_id}' disabled successfully")
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            raise

    async def delete_user(self, remna_id: int) -> bool:
        # Remnawave >= 3.0 answers a successful delete without a body.
        try:
            await self.sdk.users.delete_user(remna_id)
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            return False

        logger.info(f"RemnaUser '{remna_id}' deleted successfully")
        return True

    async def get_user_by_id(self, remna_id: Optional[int]) -> Optional[UserResponseDto]:
        if remna_id is None:  # unlinked subscription: nothing to read from the panel
            return None
        try:
            remna_user = await self.sdk.users.get_user_by_id(remna_id)
            logger.info(f"Fetched RemnaUser '{remna_id}' from panel")
            return remna_user
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            return None

    async def _stream_users(self, **filters: object) -> list[UserResponseDto]:
        # The by-telegram-id / by-email routes were removed in 3.0; the stream endpoint
        # filters server-side and pages with a keyset cursor.
        result: list[UserResponseDto] = []
        cursor: Optional[int] = None
        while True:
            response = await self.sdk.users.get_users_stream(
                size=_STREAM_PAGE_SIZE, cursor=cursor, **filters
            )
            result.extend(response.users)
            if not response.has_more or not response.next_cursor:
                break
            cursor = int(response.next_cursor)
        return result

    async def get_users_by_telegram_id(self, telegram_id: int) -> list[UserResponseDto]:
        users = await self._stream_users(telegram_id=str(telegram_id))
        logger.debug(f"Fetched {len(users)} RemnaUsers for telegram_id '{telegram_id}'")
        return users

    async def get_all_users(self, limit: int, offset: int) -> list[UserResponseDto]:
        response = await self.sdk.users.get_all_users(start=offset, size=limit)
        logger.debug(f"Fetched {len(response.users)} RemnaUsers (limit={limit}, offset={offset})")
        return response.users

    async def get_users_by_email(self, email: str) -> list[UserResponseDto]:
        users = await self._stream_users(email=email)
        logger.debug(f"Fetched {len(users)} RemnaUsers for email '{email}'")
        return users

    async def link_identity(
        self,
        remna_id: int,
        *,
        telegram_id: Optional[int] = None,
        email: Optional[str] = None,
    ) -> UserResponseDto:
        payload: dict[str, object] = {"id": remna_id}
        if telegram_id is not None:
            payload["telegram_id"] = telegram_id
        if email is not None:
            payload["email"] = email

        remna_user = await self.sdk.users.update_user(UpdateUserRequestDto(**payload))
        logger.info(
            f"RemnaUser '{remna_id}' identity linked: telegram_id={telegram_id}, email={email!r}"
        )
        return remna_user

    async def get_devices(self, remna_id: Optional[int]) -> list[HwidDeviceDto]:
        if remna_id is None:  # unlinked subscription: nothing to read from the panel
            return []
        response = await self.sdk.hwid.get_hwid_user(remna_id)
        logger.debug(f"Fetched {response.total} devices for RemnaUser '{remna_id}'")
        return response.devices if response.total else []

    async def delete_device(self, remna_id: int, hwid: str) -> Optional[int]:
        try:
            response = await self.sdk.hwid.delete_hwid_to_user(
                DeleteUserHwidDeviceRequestDto(user_id=remna_id, hwid=hwid)
            )
            logger.info(
                f"Deleted HWID device '{hwid}' for RemnaUser '{remna_id}'. "
                f"Total devices now: {response.total}"
            )
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            return None

        return int(response.total)

    async def delete_all_devices(self, remna_id: int) -> None:
        try:
            result = await self.sdk.hwid.delete_all_hwid_user(
                DeleteUserAllHwidDeviceRequestDto(user_id=remna_id)
            )
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            return
        logger.info(f"Deleted all HWID devices ({result.total}) for RemnaUser '{remna_id}'")

    async def drop_connections(self, remna_id: int) -> None:
        try:
            await self.sdk.connections.drop_connections(
                body=DropConnectionsRequestDto(
                    drop_by=DropByUserIds(user_ids=[remna_id]),
                    target_nodes=TargetAllNodes(),
                )
            )
            logger.info(f"Dropped connections for RemnaUser '{remna_id}'")
        except Exception as e:
            logger.warning(f"Failed to drop connections for RemnaUser '{remna_id}': {e}")

    async def reset_traffic(self, remna_id: int) -> Optional[UserResponseDto]:
        try:
            remna_user = await self.sdk.users.reset_user_traffic(remna_id)
            logger.info(f"Traffic for RemnaUser '{remna_user.id}' reset successfully")
            return remna_user
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")
            return None

    async def revoke_subscription(self, remna_id: int) -> None:
        try:
            await self.sdk.users.revoke_user_subscription(remna_id)
            logger.info(f"Subscription for RemnaUser '{remna_id}' revoked successfully")
        except NotFoundError:
            logger.debug(f"RemnaUser '{remna_id}' not found in panel")

    async def get_squads_available(self) -> bool:
        result = await self.sdk.internal_squads.get_internal_squads()
        return bool(result.internal_squads)

    async def get_internal_squads(self) -> list[SquadInfoDto]:
        result = await self.sdk.internal_squads.get_internal_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.internal_squads]

    async def get_external_squads(self) -> list[SquadInfoDto]:
        result = await self.sdk.external_squads.get_external_squads()
        return [SquadInfoDto(uuid=s.uuid, name=s.name) for s in result.external_squads]

    def apply_sync(self, target: T, source: Union[SubscriptionDto, RemnaSubscriptionDto]) -> T:
        if not is_dataclass(target) or not is_dataclass(source):
            raise TypeError("Both target and source must be dataclasses")

        # Explicit allowlist. The panel user id goes only into the panel reference field,
        # never into the local primary key / foreign keys (id, user_id).
        field_map = {
            "user_remna_id": "remna_id"
            if isinstance(source, RemnaSubscriptionDto)
            else "user_remna_id"
        }
        field_map.update({name: name for name in _SYNCED_FIELDS})

        target_fields = {f.name for f in fields(target)}
        source_fields = {f.name for f in fields(source)}

        for target_field, source_field in field_map.items():
            if target_field not in target_fields or source_field not in source_fields:
                continue
            old_value = getattr(target, target_field)
            new_value = getattr(source, source_field)
            if old_value != new_value:
                logger.debug(f"Field '{target_field}' changed from '{old_value}' to '{new_value}'")
                setattr(target, target_field, new_value)

        return target

    def _build_create_request(
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> CreateUserRequestDto:
        # Remnawave >= 3.0 assigns the numeric id itself; the caller stores the returned id.
        if subscription:
            return CreateUserRequestDto(
                username=user.remna_name,
                telegram_id=user.telegram_id,
                email=user.email,
                expire_at=subscription.expire_at,
                traffic_limit_strategy=subscription.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                hwid_device_limit=subscription.device_limit,
                description=user.remna_description,
                tag=_normalize_tag(subscription.tag),
                active_internal_squads=subscription.internal_squads,
                external_squad_uuid=subscription.external_squad,
            )

        if plan:
            return CreateUserRequestDto(
                username=user.remna_name,
                telegram_id=user.telegram_id,
                email=user.email,
                expire_at=days_to_datetime(plan.duration),
                traffic_limit_strategy=plan.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                description=user.remna_description,
                tag=_normalize_tag(plan.tag),
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )

        return CreateUserRequestDto(
            username=user.remna_name,
            telegram_id=user.telegram_id,
            expire_at=datetime_now() + timedelta(days=3650),
            description=user.remna_description,
            email=user.email,
        )

    def _build_update_request(
        self,
        user: UserDto,
        remna_id: int,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
    ) -> UpdateUserRequestDto:
        # remnapy sends only explicitly set fields (exclude_unset), so omitting a key leaves
        # the panel value untouched while an explicit None clears it.
        if subscription:
            payload: dict[str, object] = {
                "id": remna_id,
                "telegram_id": user.telegram_id,
                "email": user.email,
                "traffic_limit_strategy": subscription.traffic_limit_strategy,
                "traffic_limit_bytes": gb_to_bytes(subscription.traffic_limit),
                "hwid_device_limit": subscription.device_limit,
                "description": user.remna_description,
                "tag": _normalize_tag(subscription.tag),
                "active_internal_squads": subscription.internal_squads,
                "external_squad_uuid": subscription.external_squad,
            }
            # Remnawave 3.4: expireAt must be in the future and PATCH status accepts only
            # ACTIVE / DISABLED. An ordinary update of an expired user sends neither.
            if subscription.expire_at > datetime_now():
                payload["expire_at"] = subscription.expire_at
                payload["status"] = (
                    UserStatus.DISABLED
                    if subscription.status == SubscriptionStatus.DISABLED
                    else UserStatus.ACTIVE
                )
            elif subscription.status == SubscriptionStatus.DISABLED:
                payload["status"] = UserStatus.DISABLED
            return UpdateUserRequestDto(**payload)

        if plan:
            return UpdateUserRequestDto(
                id=remna_id,
                telegram_id=user.telegram_id,
                email=user.email,
                expire_at=days_to_datetime(plan.duration),
                status=UserStatus.ACTIVE,
                traffic_limit_strategy=plan.traffic_limit_strategy,
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                hwid_device_limit=plan.device_limit,
                description=user.remna_description,
                tag=_normalize_tag(plan.tag),
                active_internal_squads=plan.internal_squads,
                external_squad_uuid=plan.external_squad,
            )

        raise ValueError("Either 'plan' or 'subscription' must be provided")


def _normalize_tag(tag: Optional[str]) -> Optional[str]:
    # Remnawave 3.4 accepts null or ^[A-Z0-9_]+$ (max 16). An empty string meant "no tag".
    return tag or None
