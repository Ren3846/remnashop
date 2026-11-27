from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from pydantic import SecretStr

from src.application.services.payment_reminder import (
    REMINDER_DELAY,
    PaymentReminderService,
    format_amount,
)
from src.core.enums import TransactionStatus


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.due: dict[str, float] = {}

    async def setex(self, key, ttl, value):  # type: ignore[no-untyped-def]
        self.values[key] = value

    async def get(self, key):  # type: ignore[no-untyped-def]
        return self.values.get(key)

    async def set(self, key, value, nx=False, ex=None):  # type: ignore[no-untyped-def]
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key):  # type: ignore[no-untyped-def]
        self.values.pop(key, None)

    async def zadd(self, key, mapping):  # type: ignore[no-untyped-def]
        self.due.update(mapping)

    async def zrangebyscore(self, key, low, high):  # type: ignore[no-untyped-def]
        return [m.encode() for m, score in self.due.items() if low <= score <= high]

    async def zrem(self, key, member):  # type: ignore[no-untyped-def]
        return 1 if self.due.pop(member, None) is not None else 0


CREATED = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def make(status=TransactionStatus.PENDING, other=None, **user_kw):  # type: ignore[no-untyped-def]
    user = SimpleNamespace(
        id=7,
        telegram_id=111,
        email="buyer@example.com",
        is_email_verified=True,
        is_bot_blocked=False,
        log="[user]",
    )
    user.__dict__.update(user_kw)
    transaction = SimpleNamespace(status=status, created_at=CREATED)
    service = PaymentReminderService(
        config=SimpleNamespace(bot=SimpleNamespace(support_username=SecretStr("vivpn_help"))),
        redis=FakeRedis(),
        transaction_dao=SimpleNamespace(
            get_by_payment_id=AsyncMock(return_value=transaction),
            get_by_user=AsyncMock(return_value=[transaction, *(other or [])]),
        ),
        user_dao=SimpleNamespace(get_by_id=AsyncMock(return_value=user)),
        notifier=SimpleNamespace(notify_user=AsyncMock()),
        email_sender=SimpleNamespace(send=AsyncMock()),
    )
    return service


async def remembered(service, amount="50", now=1000.0):  # type: ignore[no-untyped-def]
    payment_id = str(uuid4())
    await service.remember(payment_id, 7, "https://pay/x", Decimal(amount), "Пробный", now=now)
    return payment_id


def test_amount_has_no_trailing_zeros() -> None:
    assert format_amount(Decimal("50.00")) == "50 ₽"
    assert format_amount(Decimal("1275")) == "1275 ₽"


async def test_link_is_mailed_once_per_amount() -> None:
    service = make()
    first = await remembered(service)
    second = await remembered(service)

    assert await service.send_link_email(first) is True
    assert await service.send_link_email(second) is False

    service.email_sender.send.assert_awaited_once()
    sent = service.email_sender.send.await_args.kwargs
    assert sent["to"] == "buyer@example.com"
    assert "https://pay/x" in sent["body"] and "50 ₽" in sent["body"]
    assert "https://t.me/vivpn_help" in sent["body"]


async def test_another_amount_gets_its_own_email() -> None:
    service = make()
    assert await service.send_link_email(await remembered(service, "50")) is True
    assert await service.send_link_email(await remembered(service, "250")) is True


async def test_unverified_email_gets_nothing() -> None:
    service = make(is_email_verified=False)
    assert await service.send_link_email(await remembered(service)) is False
    service.email_sender.send.assert_not_awaited()


async def test_failed_email_can_be_retried() -> None:
    service = make()
    payment_id = await remembered(service)
    service.email_sender.send.side_effect = RuntimeError("mailgun down")
    assert await service.send_link_email(payment_id) is False
    service.email_sender.send.side_effect = None
    assert await service.send_link_email(payment_id) is True


async def test_reminder_waits_for_the_delay() -> None:
    service = make()
    await remembered(service, now=1000.0)

    assert await service.remind_due(now=1000.0 + REMINDER_DELAY - 1) == 0
    service.notifier.notify_user.assert_not_awaited()

    assert await service.remind_due(now=1000.0 + REMINDER_DELAY) == 1
    payload = service.notifier.notify_user.await_args.args[1]
    buttons = [row[0] for row in payload.reply_markup.inline_keyboard]
    assert buttons[0].url == "https://pay/x" and "50 ₽" in buttons[0].text
    assert buttons[1].url == "https://t.me/vivpn_help"
    assert payload.delete_after is None

    # Sent once: the entry is gone.
    assert await service.remind_due(now=1000.0 + REMINDER_DELAY * 2) == 0


async def test_paid_invoice_is_not_reminded() -> None:
    service = make(status=TransactionStatus.COMPLETED)
    await remembered(service)
    assert await service.remind_due(now=10**9) == 0


async def test_cancelled_by_cleanup_is_still_reminded() -> None:
    # The bot marks an old pending transaction as cancelled while the invoice stays payable.
    service = make(status=TransactionStatus.CANCELED)
    await remembered(service)
    assert await service.remind_due(now=10**9) == 1


async def test_paid_with_another_invoice_is_not_reminded() -> None:
    later = SimpleNamespace(
        status=TransactionStatus.COMPLETED, created_at=CREATED + timedelta(minutes=3)
    )
    service = make(other=[later])
    await remembered(service)
    assert await service.remind_due(now=10**9) == 0


async def test_an_old_purchase_does_not_silence_the_reminder() -> None:
    earlier = SimpleNamespace(
        status=TransactionStatus.COMPLETED, created_at=CREATED - timedelta(days=40)
    )
    service = make(other=[earlier])
    await remembered(service)
    assert await service.remind_due(now=10**9) == 1


async def test_blocked_bot_is_skipped() -> None:
    service = make(is_bot_blocked=True)
    await remembered(service)
    assert await service.remind_due(now=10**9) == 0


async def test_one_reminder_a_day_per_user() -> None:
    service = make()
    await remembered(service, "50")
    await remembered(service, "250")
    assert await service.remind_due(now=10**9) == 1
    assert service.notifier.notify_user.await_count == 1
