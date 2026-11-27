import time
from decimal import Decimal
from typing import Optional
from uuid import UUID

import orjson
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from redis.asyncio import Redis

from src.application.common import Notifier
from src.application.common.dao import TransactionDao, UserDao
from src.application.common.email_sender import EmailSender
from src.application.dto import MessagePayloadDto
from src.core.config import AppConfig
from src.core.constants import T_ME
from src.core.enums import TransactionStatus

# An invoice is issued the moment the buyer reaches the payment screen. Many leave on the
# gateway page, so the link is also mailed at once (it opens in any browser, not only inside
# Telegram) and the buyer is nudged in the bot if the invoice is still unpaid a bit later.
REMINDER_DELAY: int = 10 * 60
_LINK_TTL: int = 24 * 60 * 60  # the gateway closes an unpaid invoice after about a day
_EMAIL_REPEAT_GUARD: int = 30 * 60
_REMINDER_REPEAT_GUARD: int = 24 * 60 * 60
_DUE_KEY = "pay_reminders"


def _link_key(payment_id: UUID | str) -> str:
    return f"pay_link:{payment_id}"


def _emailed_key(user_id: int, amount: str) -> str:
    return f"pay_link_emailed:{user_id}:{amount}"


def _reminded_key(user_id: int) -> str:
    return f"pay_reminded:{user_id}"


def format_amount(amount: Decimal) -> str:
    return f"{amount.normalize():f} ₽"


class PaymentReminderService:
    def __init__(
        self,
        config: AppConfig,
        redis: Redis,
        transaction_dao: TransactionDao,
        user_dao: UserDao,
        notifier: Notifier,
        email_sender: EmailSender,
    ) -> None:
        self.config = config
        self.redis = redis
        self.transaction_dao = transaction_dao
        self.user_dao = user_dao
        self.notifier = notifier
        self.email_sender = email_sender

    async def remember(
        self,
        payment_id: UUID | str,
        user_id: int,
        url: str,
        amount: Decimal,
        plan_name: str,
        now: Optional[float] = None,
    ) -> None:
        """Keeps the payment link (the transaction does not store it) and queues the nudge."""
        record = {
            "user_id": user_id,
            "url": url,
            "amount": format_amount(amount),
            "plan": plan_name,
        }
        await self.redis.setex(_link_key(payment_id), _LINK_TTL, orjson.dumps(record))
        due_at = (now if now is not None else time.time()) + REMINDER_DELAY
        await self.redis.zadd(_DUE_KEY, {str(payment_id): due_at})

    async def send_link_email(self, payment_id: UUID | str) -> bool:
        record = await self._load(payment_id)
        if record is None:
            return False

        user = await self.user_dao.get_by_id(record["user_id"])
        if not user or not user.email or not user.is_email_verified:
            return False

        # Switching the term on the payment screen issues a new invoice each time.
        guard = _emailed_key(user.id, record["amount"])
        if not await self.redis.set(guard, b"1", nx=True, ex=_EMAIL_REPEAT_GUARD):
            return False

        body = (
            "Здравствуйте!\n\n"
            f"Вы оформляете подписку ViVPN «{record['plan']}» за {record['amount']}.\n\n"
            "Ссылка на оплату:\n"
            f"{record['url']}\n\n"
            "Ссылка действует сутки. Если страница оплаты не открылась в Telegram, "
            "откройте её в обычном браузере.\n\n"
            "Доступ появится в боте сразу после оплаты.\n\n"
            f"Не получается оплатить? Напишите нам: {self._support_url()}\n\n"
            "Если вы не оформляли подписку, просто не отвечайте на это письмо."
        )
        try:
            await self.email_sender.send(to=user.email, subject="Ссылка на оплату ViVPN", body=body)
        except Exception as e:
            await self.redis.delete(guard)
            logger.error(f"Payment link for '{payment_id}' was not mailed to {user.log}: {e}")
            return False

        logger.info(f"Payment link for '{payment_id}' mailed to {user.log}")
        return True

    async def remind_due(self, now: Optional[float] = None) -> int:
        """Nudges in the bot everyone whose invoice is still unpaid. Returns how many."""
        moment = now if now is not None else time.time()
        due = await self.redis.zrangebyscore(_DUE_KEY, 0, moment)
        sent = 0
        for raw in due:
            payment_id = raw.decode() if isinstance(raw, bytes) else str(raw)
            # Whoever removes the entry owns it: a second run must not send it again.
            if not await self.redis.zrem(_DUE_KEY, payment_id):
                continue
            try:
                if await self._remind(payment_id):
                    sent += 1
            except Exception as e:
                logger.error(f"Reminder for payment '{payment_id}' failed: {e}")
        return sent

    async def _remind(self, payment_id: str) -> bool:
        record = await self._load(payment_id)
        if record is None:
            return False

        transaction = await self.transaction_dao.get_by_payment_id(UUID(payment_id))
        if not transaction or transaction.status == TransactionStatus.COMPLETED:
            return False

        user = await self.user_dao.get_by_id(record["user_id"])
        if not user or user.telegram_id is None or user.is_bot_blocked:
            return False

        # Paid through another invoice in the meantime: nothing to remind about.
        for other in await self.transaction_dao.get_by_user(user.id):
            if other.status != TransactionStatus.COMPLETED:
                continue
            if not transaction.created_at or not other.created_at:
                return False
            if other.created_at >= transaction.created_at:
                return False

        if not await self.redis.set(
            _reminded_key(user.id), b"1", nx=True, ex=_REMINDER_REPEAT_GUARD
        ):
            return False

        text = (
            "💳 <b>Оплата не завершена</b>\n\n"
            f"Вы выбрали «{record['plan']}» за {record['amount']}, но оплата не прошла. "
            "Ссылка ещё действует — нажмите кнопку ниже. Мы также отправили её вам на почту.\n\n"
            "Если страница оплаты не открылась или что-то пошло не так, напишите нам — поможем."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {record['amount']}", url=record["url"])],
                [InlineKeyboardButton(text="💬 Написать в поддержку", url=self._support_url())],
            ]
        )
        await self.notifier.notify_user(
            user,
            MessagePayloadDto(
                i18n_key="raw-message",
                i18n_kwargs={"content": text},
                reply_markup=keyboard,
                disable_default_markup=True,
                delete_after=None,
            ),
        )
        logger.info(f"Unpaid invoice '{payment_id}': reminder sent to {user.log}")
        return True

    async def _load(self, payment_id: UUID | str) -> Optional[dict]:
        raw = await self.redis.get(_link_key(payment_id))
        return orjson.loads(raw) if raw else None

    def _support_url(self) -> str:
        return f"{T_ME}{self.config.bot.support_username.get_secret_value()}"
