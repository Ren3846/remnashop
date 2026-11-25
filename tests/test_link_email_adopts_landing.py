import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.application.use_cases.remnawave.commands.synchronization import SyncRemnaUser
from src.application.use_cases.user.commands.link_email import LinkUserEmail
from src.core.exceptions import EmailAlreadyUsedError

NOW = datetime.now(timezone.utc)


def panel_user(remna_id: int, days: int, telegram_id=None, status="ACTIVE"):
    return SimpleNamespace(
        id=remna_id, status=status, expire_at=NOW + timedelta(days=days), telegram_id=telegram_id
    )


def link_email(bound, by_email):
    calls = SimpleNamespace(cleared=[], identity=[], synced=[])

    async def get_current(user_id):
        return SimpleNamespace(user_remna_id=bound.id) if bound else None

    async def get_user_by_id(remna_id):
        return bound

    async def get_users_by_email(email):
        return by_email

    async def get_users_by_telegram_id(telegram_id):
        return [bound] if bound else []

    async def link_identity(remna_id, *, telegram_id=None, email=None):
        calls.identity.append(remna_id)
        return SimpleNamespace(id=remna_id, telegram_id=telegram_id, linked=True)

    async def clear_current_subscription(user_id):
        calls.cleared.append(user_id)

    async def sync(dto):
        # Must be the panel user as returned after linking, with the Telegram id on it.
        assert dto.remna_user.linked and dto.remna_user.telegram_id == 555
        calls.synced.append(dto.remna_user.id)

    interactor = object.__new__(LinkUserEmail)
    interactor.subscription_dao = SimpleNamespace(get_current=get_current)
    interactor.user_dao = SimpleNamespace(clear_current_subscription=clear_current_subscription)
    interactor.remnawave = SimpleNamespace(
        get_user_by_id=get_user_by_id,
        get_users_by_email=get_users_by_email,
        get_users_by_telegram_id=get_users_by_telegram_id,
        link_identity=link_identity,
    )
    interactor.sync_remna_user = SimpleNamespace(system=sync)
    user = SimpleNamespace(id=80, telegram_id=555, log="[user]")
    asyncio.run(interactor._sync_remna_identity(user, "buyer@example.com"))
    return calls


def test_longer_landing_purchase_becomes_the_current_subscription() -> None:
    old = panel_user(16, days=14, telegram_id=555)
    # Paid twice on the landing: the one that runs longest is taken.
    calls = link_email(bound=old, by_email=[old, panel_user(44, 30), panel_user(45, 31)])
    assert calls.cleared == [80]
    assert calls.identity == [45]
    assert calls.synced == [45]


def test_shorter_or_inactive_landing_purchase_changes_nothing() -> None:
    old = panel_user(16, days=60, telegram_id=555)
    for landing in (panel_user(44, 30), panel_user(44, 90, status="EXPIRED")):
        calls = link_email(bound=old, by_email=[landing])
        assert calls.cleared == []
        assert calls.identity == [16]
        assert calls.synced == []


def test_user_without_a_subscription_still_gets_the_landing_one() -> None:
    calls = link_email(bound=None, by_email=[panel_user(44, 30)])
    assert calls.cleared == []
    assert calls.synced == [44]


def test_landing_purchase_claimed_by_someone_else_is_refused() -> None:
    old = panel_user(16, days=14, telegram_id=555)
    with pytest.raises(EmailAlreadyUsedError):
        link_email(bound=old, by_email=[panel_user(44, 30, telegram_id=999)])


def sync_guard(bound_id, bound_exists: bool) -> SyncRemnaUser:
    async def get_current(user_id):
        return SimpleNamespace(user_remna_id=bound_id) if bound_id is not None else None

    async def get_user_by_id(remna_id):
        return SimpleNamespace(id=remna_id) if bound_exists else None

    interactor = object.__new__(SyncRemnaUser)
    interactor.subscription_dao = SimpleNamespace(get_current=get_current)
    interactor.remnawave = SimpleNamespace(get_user_by_id=get_user_by_id)
    return interactor


def test_second_panel_user_cannot_overwrite_the_bound_subscription() -> None:
    # The old panel user shares the Telegram id, but the bot subscription is bound to 45.
    assert asyncio.run(sync_guard(45, True)._is_bound_to_another_panel_user(80, 16)) is True
    assert asyncio.run(sync_guard(45, True)._is_bound_to_another_panel_user(80, 45)) is False


def test_recreated_or_first_panel_user_is_still_synced() -> None:
    # Bound panel user was deleted and re-created under a new id: sync must rebind.
    assert asyncio.run(sync_guard(16, False)._is_bound_to_another_panel_user(80, 17)) is False
    assert asyncio.run(sync_guard(None, True)._is_bound_to_another_panel_user(80, 17)) is False


def plan_stub(name, gb, devices, squads=("sq",), days=(30, 90), trial=False):
    return SimpleNamespace(
        id=3,
        name=name,
        tag=None,
        type="BOTH",
        traffic_limit_strategy="MONTH_ROLLING",
        traffic_limit=gb,
        device_limit=devices,
        internal_squads=list(squads),
        external_squad=None,
        is_trial=trial,
        durations=[SimpleNamespace(days=d) for d in days],
    )


def matching(plans, gb, devices, squads=("sq",), days_left=29):
    async def get_all():
        return plans

    interactor = object.__new__(SyncRemnaUser)
    interactor.plan_dao = SimpleNamespace(get_all=get_all)
    subscription = SimpleNamespace(
        traffic_limit=gb,
        device_limit=devices,
        internal_squads=list(squads),
        expire_at=NOW + timedelta(days=days_left, hours=12),
    )
    return asyncio.run(interactor._matching_plan_snapshot(subscription))


def test_imported_landing_subscription_gets_its_plan_name() -> None:
    plans = [plan_stub("Базовый", 600, 3), plan_stub("Расширенный", 1200, 6)]
    snapshot = matching(plans, 600, 3)
    assert (snapshot.name, snapshot.duration) == ("Базовый", 30)
    # A yearly purchase must not be renewed as a monthly one by mistake.
    assert matching(plans, 600, 3, days_left=80).duration == 90


def test_unrecognised_or_ambiguous_subscription_stays_a_plain_import() -> None:
    plans = [plan_stub("Базовый", 600, 3), plan_stub("Двойник", 600, 3)]
    assert matching(plans, 600, 3) is None
    assert matching([plan_stub("Базовый", 600, 3)], 100, 1) is None
    assert matching([plan_stub("Базовый", 600, 3)], 600, 3, squads=("other",)) is None
    assert matching([plan_stub("Пробный", 600, 3, trial=True)], 600, 3) is None
