import asyncio
from types import SimpleNamespace
from uuid import uuid4

import orjson
import pytest

from src.application.services.landing_payment import (
    LandingPaymentService,
    landing_redis_key,
    normalize_source,
)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def getdel(self, key: str):
        return self.data.pop(key, None)

    async def setex(self, key: str, ttl: int, value: bytes) -> None:
        self.data[key] = value


class FakeUsers:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests = []

    async def create_user(self, request):
        if self.fail:
            raise RuntimeError("panel down")
        self.requests.append(request)
        return SimpleNamespace(id=77, subscription_url="https://sub.example/abc")


def make_service(redis: FakeRedis, users: FakeUsers) -> LandingPaymentService:
    service = LandingPaymentService(
        config=SimpleNamespace(mailgun=None),
        redis=redis,
        payment_gateway_dao=None,
        plan_dao=None,
        remnawave_sdk=SimpleNamespace(users=users),
        retort=None,
        bot_service=None,
    )
    service.sent = []

    async def fake_send(email: str, subscription_url: str, days: int) -> None:
        service.sent.append((email, subscription_url, days))

    service._send_activation_email = fake_send
    return service


def order(source=None) -> bytes:
    return orjson.dumps({"email": "buyer@example.com", "days": 30, "source": source})


def test_fulfil_creates_user_once_and_sends_email() -> None:
    redis, users = FakeRedis(), FakeUsers()
    payment_id = uuid4()
    redis.data[landing_redis_key(payment_id)] = order("tg_ads")
    service = make_service(redis, users)

    assert asyncio.run(service.try_fulfill(payment_id)) is True
    # A repeated webhook for the same contract must not create a second panel user.
    assert asyncio.run(service.try_fulfill(payment_id)) is False

    assert len(users.requests) == 1
    request = users.requests[0]
    assert request.email == "buyer@example.com"
    assert request.description == "landing; source=tg_ads"
    assert request.tag is None
    assert service.sent == [("buyer@example.com", "https://sub.example/abc", 30)]


def test_unknown_payment_is_not_a_landing_order() -> None:
    service = make_service(FakeRedis(), FakeUsers())
    assert asyncio.run(service.try_fulfill(uuid4())) is False


def test_order_is_restored_when_panel_fails() -> None:
    redis, users = FakeRedis(), FakeUsers(fail=True)
    payment_id = uuid4()
    redis.data[landing_redis_key(payment_id)] = order()
    service = make_service(redis, users)

    with pytest.raises(RuntimeError):
        asyncio.run(service.try_fulfill(payment_id))

    assert landing_redis_key(payment_id) in redis.data
    assert service.sent == []


def test_normalize_source() -> None:
    assert normalize_source(None) is None
    assert normalize_source("  ") is None
    assert normalize_source("tg-ads_1.a") == "tg-ads_1.a"
    assert normalize_source("<script>x</script>") == "_script_x__script_"
    assert len(normalize_source("a" * 500)) == 48


class FakePlan(SimpleNamespace):
    def get_duration(self, days: int):
        return days if days in self.days else None


def plan(**kw) -> FakePlan:
    base = dict(name="p", is_trial=False, availability="ALL", days=[30], internal_squads=["sq"])
    return FakePlan(**{**base, **kw})


def resolve(plans, monkeypatch) -> str:
    from src.application.services import landing_payment as module

    monkeypatch.setattr(
        module.PlanSnapshotDto, "from_plan", staticmethod(lambda p, days: p.name)
    )

    async def get_active_plans():
        return plans

    service = make_service(FakeRedis(), FakeUsers())
    service.plan_dao = SimpleNamespace(get_active_plans=get_active_plans)
    return asyncio.run(service._resolve_plan_snapshot(30))


def test_public_plan_with_squads_is_used(monkeypatch) -> None:
    plans = [
        plan(name="trial", is_trial=True),
        plan(name="by-link", availability="LINK"),
        plan(name="no-squads", internal_squads=[]),
        plan(name="other-duration", days=[90]),
        plan(name="public"),
    ]
    assert resolve(plans, monkeypatch) == "public"


def test_no_sellable_plan_refuses_payment(monkeypatch) -> None:
    with pytest.raises(RuntimeError):
        resolve([plan(is_trial=True), plan(internal_squads=[])], monkeypatch)
