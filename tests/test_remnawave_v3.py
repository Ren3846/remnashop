"""Remnawave 3.4.4 compatibility: numeric ids, sync allowlist, PATCH rules, expiration meta."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from remnapy.enums import TrafficLimitStrategy, UserStatus

from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SubscriptionDto,
    UnlinkedSubscriptionError,
    UserDto,
)
from src.application.services.remnawave import RemnaUserEvent, RemnaWebhookService
from src.core.enums import PlanType, SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.services.remnawave import RemnawaveImpl


def _plan() -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=1, name="p", type=PlanType.BOTH, traffic_limit=100, device_limit=1, duration=30
    )


def _sub(**kw: object) -> SubscriptionDto:
    base: dict[str, object] = {
        "id": 7,
        "user_id": 3,
        "user_remna_id": 42,
        "traffic_limit": 100,
        "device_limit": 1,
        "traffic_limit_strategy": TrafficLimitStrategy.NO_RESET,
        "expire_at": datetime_now() + timedelta(days=10),
        "url": "https://sub/old",
        "plan_snapshot": _plan(),
    }
    base.update(kw)
    return SubscriptionDto(**base)  # type: ignore[arg-type]


def _remna_sub(**kw: object) -> RemnaSubscriptionDto:
    base: dict[str, object] = {
        "remna_id": 99,
        "status": SubscriptionStatus.ACTIVE,
        "expire_at": datetime_now() + timedelta(days=20),
        "url": "https://sub/new",
        "traffic_limit": 50,
        "device_limit": 2,
        "traffic_limit_strategy": TrafficLimitStrategy.NO_RESET,
    }
    base.update(kw)
    return RemnaSubscriptionDto(**base)  # type: ignore[arg-type]


def _user() -> UserDto:
    return UserDto(id=3, telegram_id=1001, name="u", referral_code="r")


@pytest.fixture
def remnawave() -> RemnawaveImpl:
    return RemnawaveImpl(sdk=MagicMock())


# --- sync_foreign_row -----------------------------------------------------------------


def test_sync_copies_panel_id_only_into_reference_field(remnawave: RemnawaveImpl) -> None:
    # Panel id of one subscription equals the local PK of another one.
    target = _sub(id=99, user_id=5, user_remna_id=7)
    other = _sub(id=7, user_id=6, user_remna_id=99)
    source = _remna_sub(remna_id=8)

    result = remnawave.apply_sync(target, source)

    assert result.id == 99 and result.user_id == 5  # PK / FK untouched
    assert result.user_remna_id == 8
    assert result.url == "https://sub/new" and result.device_limit == 2
    assert result.plan_snapshot == _plan()  # not a synced field
    assert (other.id, other.user_id, other.user_remna_id) == (7, 6, 99)


# --- update_payload --------------------------------------------------------------------


def test_update_active_sends_id_status_and_future_expiry(remnawave: RemnawaveImpl) -> None:
    dto = remnawave._build_update_request(_user(), 42, None, _sub())
    body = dto.model_dump(exclude_unset=True, by_alias=True, mode="json")
    assert body["id"] == 42 and "uuid" not in body
    assert body["status"] == UserStatus.ACTIVE
    assert "expireAt" in body


def test_update_expired_user_sends_no_status_and_no_past_date(remnawave: RemnawaveImpl) -> None:
    sub = _sub(status=SubscriptionStatus.EXPIRED, expire_at=datetime_now() - timedelta(days=1))
    body = remnawave._build_update_request(_user(), 42, None, sub).model_dump(
        exclude_unset=True, by_alias=True, mode="json"
    )
    assert "status" not in body and "expireAt" not in body


def test_update_disabled_expired_user_keeps_disabled(remnawave: RemnawaveImpl) -> None:
    sub = _sub(status=SubscriptionStatus.DISABLED, expire_at=datetime_now() - timedelta(days=1))
    body = remnawave._build_update_request(_user(), 42, None, sub).model_dump(
        exclude_unset=True, by_alias=True, mode="json"
    )
    assert body["status"] == UserStatus.DISABLED and "expireAt" not in body


@pytest.mark.parametrize(("tag", "expected"), [("", None), (None, None), ("VIP_1", "VIP_1")])
def test_update_tag_normalized(remnawave: RemnawaveImpl, tag: object, expected: object) -> None:
    body = remnawave._build_update_request(_user(), 42, None, _sub(tag=tag)).model_dump(
        exclude_unset=True, by_alias=True, mode="json"
    )
    assert "tag" in body and body["tag"] == expected  # explicit clear stays explicit


def test_create_request_has_no_client_side_id(remnawave: RemnawaveImpl) -> None:
    body = remnawave._build_create_request(_user(), None, _sub()).model_dump(
        exclude_unset=True, by_alias=True, mode="json"
    )
    assert "uuid" not in body and "id" not in body


# --- unlinked subscriptions ------------------------------------------------------------


def test_unlinked_subscription_never_yields_a_panel_id() -> None:
    sub = _sub(user_remna_id=None)
    with pytest.raises(UnlinkedSubscriptionError):
        _ = sub.remna_id


async def test_reads_for_unlinked_subscription_do_not_call_panel(remnawave: RemnawaveImpl) -> None:
    remnawave.sdk.users.get_user_by_id = AsyncMock()
    remnawave.sdk.hwid.get_hwid_user = AsyncMock()
    assert await remnawave.get_user_by_id(None) is None
    assert await remnawave.get_devices(None) == []
    remnawave.sdk.users.get_user_by_id.assert_not_awaited()
    remnawave.sdk.hwid.get_hwid_user.assert_not_awaited()


# --- lookups replaced by the stream endpoint -------------------------------------------


async def test_get_users_by_telegram_id_pages_through_stream(remnawave: RemnawaveImpl) -> None:
    pages = [
        SimpleNamespace(users=["a"], has_more=True, next_cursor="17"),
        SimpleNamespace(users=["b"], has_more=False, next_cursor=None),
    ]
    remnawave.sdk.users.get_users_stream = AsyncMock(side_effect=pages)
    assert await remnawave.get_users_by_telegram_id(1001) == ["a", "b"]
    calls = remnawave.sdk.users.get_users_stream.await_args_list
    assert calls[0].kwargs["telegram_id"] == "1001" and calls[0].kwargs["cursor"] is None
    assert calls[1].kwargs["cursor"] == 17


# --- expiration_webhook ----------------------------------------------------------------


def _service() -> RemnaWebhookService:
    svc = RemnaWebhookService.__new__(RemnaWebhookService)
    svc.event_bus = MagicMock(publish=AsyncMock())
    return svc


@pytest.mark.parametrize(("hours", "day"), [(-72, 3), (-48, 2), (-24, 1)])
async def test_expiration_before_expiry_maps_to_expires_in(hours: int, day: int) -> None:
    svc = _service()
    sub = _sub()
    remna_user = SimpleNamespace(id=42, telegram_id=1001, expire_at=sub.expire_at)
    await svc._process_expiration(_user(), sub, remna_user, SimpleNamespace(expiration=hours))  # type: ignore[arg-type]
    event = svc.event_bus.publish.await_args.args[0]
    assert type(event).__name__ == "SubscriptionExpiresEvent" and event.day == day


async def test_expiration_after_expiry_maps_to_expired_ago() -> None:
    svc = _service()
    sub = _sub()
    remna_user = SimpleNamespace(id=42, telegram_id=1001, expire_at=sub.expire_at)
    await svc._process_expiration(_user(), sub, remna_user, SimpleNamespace(expiration=24))  # type: ignore[arg-type]
    event = svc.event_bus.publish.await_args.args[0]
    assert type(event).__name__ == "SubscriptionExpiredAgoEvent" and event.day == 1


@pytest.mark.parametrize(
    "meta", [None, SimpleNamespace(expiration=None), SimpleNamespace(expiration=-12)]
)
async def test_expiration_unknown_offset_is_not_guessed(meta: object) -> None:
    svc = _service()
    sub = _sub()
    remna_user = SimpleNamespace(id=42, telegram_id=1001, expire_at=sub.expire_at)
    await svc._process_expiration(_user(), sub, remna_user, meta)  # type: ignore[arg-type]
    svc.event_bus.publish.assert_not_awaited()


def test_expiration_event_name_matches_panel() -> None:
    assert RemnaUserEvent.EXPIRATION == "user.expiration"
