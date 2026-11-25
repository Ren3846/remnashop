import re
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import orjson
from adaptix import Retort
from httpx import AsyncClient, HTTPStatusError
from loguru import logger
from redis.asyncio import Redis
from remnapy import RemnawaveSDK
from remnapy.enums.users import UserStatus
from remnapy.models import CreateUserRequestDto, UpdateUserRequestDto

from src.application.common import BotService
from src.application.common.dao import PaymentGatewayDao, PlanDao
from src.application.dto import PlanDto, PlanSnapshotDto
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.core.config import AppConfig
from src.core.config.mailgun import MailgunConfig
from src.core.constants import (
    APP_STORE_REGION_GUIDE_URL,
    HAPP_ANDROID_APK_URL,
    HAPP_IOS_URL,
    INCY_ANDROID_URL,
    INCY_IOS_URL,
    RABBITHOLE_IOS_URL,
)
from src.core.enums import Currency, PaymentGatewayType, PlanAvailability, PlanType
from src.core.utils.converters import days_to_datetime, gb_to_bytes
from src.core.utils.time import datetime_now
from src.infrastructure.payment_gateways.lava import LavaOfferNotFoundError, resolve_lava_offer

_REDIS_PREFIX = "web_payment:"
# A Lava invoice can be paid long after it was created; keep the order for a week.
_REDIS_TTL = 60 * 60 * 24 * 7
# A paid landing subscription is remembered for as long as Lava may keep charging for it:
# the longest period is a year, and every renewal pushes the record forward.
_SUBSCRIPTION_TTL = 60 * 60 * 24 * 400
_SOURCE_RE = re.compile(r"[^a-zA-Z0-9_.-]")
_SOURCE_MAX_LEN = 48
_PUBLIC_AVAILABILITY = (PlanAvailability.ALL, PlanAvailability.NEW)


def landing_redis_key(payment_id: UUID | str) -> str:
    return f"{_REDIS_PREFIX}{payment_id}"


def landing_subscription_key(payment_id: UUID | str) -> str:
    return f"web_subscription:{payment_id}"


def landing_renewal_key(payment_id: UUID | str) -> str:
    return f"web_renewal:{payment_id}"


def normalize_source(source: Optional[str]) -> Optional[str]:
    """Traffic source label from the landing (utm_source / ref): safe chars only."""
    if not source:
        return None
    cleaned = _SOURCE_RE.sub("_", source.strip())[:_SOURCE_MAX_LEN]
    return cleaned or None


class LandingPaymentService:
    def __init__(
        self,
        config: AppConfig,
        redis: Redis,
        payment_gateway_dao: PaymentGatewayDao,
        plan_dao: PlanDao,
        remnawave_sdk: RemnawaveSDK,
        retort: Retort,
        bot_service: BotService,
    ) -> None:
        self.config = config
        self.redis = redis
        self.payment_gateway_dao = payment_gateway_dao
        self.plan_dao = plan_dao
        self.remnawave_sdk = remnawave_sdk
        self.retort = retort
        self.bot_service = bot_service

    async def get_public_plans(self) -> list[dict[str, Any]]:
        return [
            {
                "code": plan.public_code,
                "name": plan.name,
                "devices": plan.device_limit,
                "trafficGb": plan.traffic_limit,
                "durations": [
                    {"days": d.days, "price": float(d.get_price(Currency.RUB))}
                    for d in plan.durations
                ],
            }
            for plan in await self._public_plans()
        ]

    async def create_lava_payment_link(
        self,
        email: str,
        days: int,
        plan_code: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        gateway = await self.payment_gateway_dao.get_by_type(PaymentGatewayType.LAVA)
        if (
            not gateway
            or not gateway.is_active
            or not isinstance(gateway.settings, LavaGatewaySettingsDto)
        ):
            raise RuntimeError("Lava payment gateway is not configured")

        settings: LavaGatewaySettingsDto = gateway.settings
        api_key = settings.api_key.get_secret_value()  # type: ignore[union-attr]

        plan_snapshot, amount = await self._resolve_plan_snapshot(days, plan_code)
        source = normalize_source(source)

        async with AsyncClient(
            base_url="https://gate.lava.top",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=15,
        ) as client:
            try:
                # Never charge a price other than the one shown on the site.
                resolved_offer_id, lava_periodicity = await resolve_lava_offer(client, amount)
            except LavaOfferNotFoundError as e:
                logger.critical(f"Landing plan '{plan_snapshot.name}' for {days} days: {e}")
                raise RuntimeError(str(e)) from e
            except HTTPStatusError as e:
                raise RuntimeError(
                    f"Lava payment failed ({e.response.status_code}): {e.response.text}"
                ) from e

            payload = {
                "offerId": resolved_offer_id,
                "email": email,
                "currency": "RUB",
                "periodicity": lava_periodicity,
                "buyerLanguage": "RU",
            }

            try:
                response = await client.post("/api/v2/invoice", content=orjson.dumps(payload))
                if response.status_code >= 400:
                    logger.error(
                        f"Lava.top API error {response.status_code} for landing payment: "
                        f"{response.text}"
                    )
                response.raise_for_status()
            except HTTPStatusError as e:
                raise RuntimeError(
                    f"Lava payment failed ({e.response.status_code}): {e.response.text}"
                ) from e

            data = orjson.loads(response.content)

        payment_id = data["id"]
        payment_url = data["paymentUrl"]
        order_id = data.get("orderId", payment_id)

        await self.redis.setex(
            landing_redis_key(payment_id),
            _REDIS_TTL,
            orjson.dumps(
                {
                    "email": email,
                    "days": days,
                    "offer_id": resolved_offer_id,
                    "periodicity": lava_periodicity,
                    "plan_snapshot": self.retort.dump(plan_snapshot),
                    "amount": float(amount),
                    "source": source,
                }
            ),
        )

        logger.info(
            f"Landing payment created: id={payment_id}, email={email}, "
            f"plan='{plan_snapshot.name}', days={days}, amount={amount}, "
            f"offer={resolved_offer_id}, source={source}"
        )

        return {
            "days": days,
            "payUrl": payment_url,
            "invoiceId": payment_id,
            "orderId": order_id,
        }

    async def try_fulfill(self, payment_id: UUID) -> bool:
        key = landing_redis_key(payment_id)
        # Atomic claim: Lava repeats webhooks, only one delivery may create the panel user.
        raw = await self.redis.getdel(key)
        if not raw:
            return False

        payment_data = orjson.loads(raw)
        email: str = payment_data["email"]
        days: int = payment_data.get("days", 30)
        plan_data = payment_data.get("plan_snapshot")
        source: Optional[str] = payment_data.get("source")

        plan_snapshot = (
            self.retort.load(plan_data, PlanSnapshotDto)
            if plan_data
            else self._default_plan_snapshot(days)
        )
        plan_snapshot.duration = days
        username = self._build_username(email)

        try:
            request = self._build_create_request(
                email=email, username=username, plan=plan_snapshot, source=source
            )
            remna_user = await self.remnawave_sdk.users.create_user(request)
            subscription_url = remna_user.subscription_url
            logger.info(
                f"Landing Remnawave user '{username}' (id={remna_user.id}) created for "
                f"email={email}, payment={payment_id}, source={source}"
            )
        except Exception as e:
            # Paid but not delivered: put the order back so a webhook retry can fulfil it.
            await self.redis.setex(key, _REDIS_TTL, raw)
            logger.exception(f"Failed to create Remnawave user for landing email={email}: {e}")
            raise

        # Lava charges this subscription again on its own; the renewal is matched to the
        # panel user by this record (never by email or username).
        await self.redis.setex(
            landing_subscription_key(payment_id),
            _SUBSCRIPTION_TTL,
            orjson.dumps({"email": email, "days": days, "remna_id": remna_user.id}),
        )

        try:
            await self._send_activation_email(
                email=email, subscription_url=subscription_url, days=days
            )
        except Exception as e:
            # The panel user exists; never re-create it. Surface for manual resend.
            logger.critical(
                f"Landing user '{username}' created but activation email to {email} failed: {e}"
            )
            raise
        return True

    async def try_renew(self, payment_id: UUID, parent_payment_id: UUID) -> bool:
        """Extend a landing subscription after a recurring Lava charge.

        Returns False when the first payment was not a landing purchase.
        """
        subscription_key = landing_subscription_key(parent_payment_id)
        raw = await self.redis.get(subscription_key)
        if not raw:
            return False

        # Lava repeats webhooks: one charge extends the access once.
        renewal_key = landing_renewal_key(payment_id)
        if not await self.redis.set(renewal_key, b"1", nx=True, ex=_SUBSCRIPTION_TTL):
            return True

        record = orjson.loads(raw)
        email: str = record["email"]
        days: int = record["days"]
        remna_id: int = record["remna_id"]

        try:
            remna_user = await self.remnawave_sdk.users.get_user_by_id(remna_id)
            expire_at = max(remna_user.expire_at, datetime_now()) + timedelta(days=days)
            await self.remnawave_sdk.users.update_user(
                UpdateUserRequestDto(id=remna_id, expire_at=expire_at, status=UserStatus.ACTIVE)
            )
        except Exception as e:
            # Charged but not extended: release the claim so a webhook retry can do it.
            await self.redis.delete(renewal_key)
            logger.critical(
                f"Recurring payment '{payment_id}' for landing user id={remna_id} "
                f"({email}) was charged but not extended: {e}"
            )
            raise

        await self.redis.expire(subscription_key, _SUBSCRIPTION_TTL)
        logger.info(
            f"Landing user id={remna_id} ({email}) extended by {days} days until "
            f"{expire_at:%Y-%m-%d}, payment={payment_id}"
        )

        try:
            await self._send_email(
                email=email,
                subject="Ваша VPN-подписка продлена",
                html=(
                    "<h2>Подписка продлена</h2>"
                    f"<p>Оплата прошла, доступ продлён на <b>{days} дней</b> — "
                    f"до {expire_at:%d.%m.%Y}.</p>"
                    "<p>Ничего настраивать не нужно: ссылка-ключ в приложении остаётся прежней.</p>"
                ),
            )
        except Exception as e:
            logger.error(f"Renewal email to {email} failed: {e}")
        return True

    async def _public_plans(self) -> list[PlanDto]:
        # The landing sells public plans only. Trial, by-link and per-user plans are skipped,
        # and so is a plan without squads: it would give a paid user no servers.
        plans = []
        for plan in await self.plan_dao.get_active_plans():
            if plan.is_trial or plan.availability not in _PUBLIC_AVAILABILITY:
                continue
            if not plan.internal_squads:
                logger.error(f"Plan '{plan.name}' has no internal squads, not sold on landing")
                continue
            plans.append(plan)
        return plans

    async def _resolve_plan_snapshot(
        self, days: int, plan_code: Optional[str] = None
    ) -> tuple[PlanSnapshotDto, Decimal]:
        for plan in await self._public_plans():
            if plan_code and plan.public_code != plan_code:
                continue
            duration = plan.get_duration(days)
            if not duration:
                continue
            return PlanSnapshotDto.from_plan(plan, days), duration.get_price(Currency.RUB)

        raise RuntimeError(
            f"No public plan with squads is configured for {days} days (plan={plan_code!r})"
        )

    @staticmethod
    def _default_plan_snapshot(days: int) -> PlanSnapshotDto:
        return PlanSnapshotDto(
            id=-1,
            name="landing",
            type=PlanType.BOTH,
            traffic_limit=100,
            device_limit=1,
            duration=days,
        )

    @staticmethod
    def _build_username(email: str) -> str:
        local = re.sub(r"[^a-zA-Z0-9]", "_", email.split("@")[0])[:20]
        return f"web_{local}_{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _build_create_request(
        email: str,
        username: str,
        plan: PlanSnapshotDto,
        source: Optional[str] = None,
    ) -> CreateUserRequestDto:
        return CreateUserRequestDto(
            username=username,
            email=email,
            description=f"landing; source={source or 'direct'}",
            expire_at=days_to_datetime(plan.duration),
            traffic_limit_strategy=plan.traffic_limit_strategy,
            traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
            hwid_device_limit=plan.device_limit,
            tag=plan.tag or None,  # Remnawave 3.4: null or ^[A-Z0-9_]+$, never ""
            active_internal_squads=plan.internal_squads,
            external_squad_uuid=plan.external_squad,
        )

    async def _send_activation_email(self, email: str, subscription_url: str, days: int) -> None:
        link_url = await self.bot_service.get_link_email_url()
        html = (
            "<h2>Спасибо за покупку!</h2>"
            f"<p>Ваша VPN-подписка активирована на <b>{days} дней</b>.</p>"
            "<p><b>Как подключиться:</b></p>"
            "<ol>"
            "<li>Установите приложение."
            "<br><b>iPhone, iPad, Mac.</b> Happ и INCY удалены из российского App Store. "
            f"<a href='{APP_STORE_REGION_GUIDE_URL}'>Смените страну в App Store</a> "
            f"и установите <a href='{HAPP_IOS_URL}'>Happ</a> или "
            f"<a href='{INCY_IOS_URL}'>INCY</a>. Если сменить страну не получается, "
            f"установите <a href='{RABBITHOLE_IOS_URL}'>RabbitHole</a>: "
            "он есть в российском App Store."
            "<br><b>Android.</b> Happ в Google Play нет: скачайте и откройте файл "
            f"<a href='{HAPP_ANDROID_APK_URL}'>Happ.apk</a>. "
            f"Или установите <a href='{INCY_ANDROID_URL}'>INCY из Google Play</a>.</li>"
            "<li>Скопируйте вашу ссылку-ключ:<br>"
            f"<a href='{subscription_url}'>{subscription_url}</a></li>"
            "<li>В приложении нажмите «+» и выберите вставку из буфера обмена.</li>"
            "<li>Выберите сервер и нажмите кнопку подключения.</li>"
            "</ol>"
            "<p>Чтобы управлять подпиской и продлевать её в Telegram, откройте бота "
            "и укажите этот же email:</p>"
            f"<p><a href='{link_url}'>{link_url}</a></p>"
            "<p>Сохраните это письмо: ссылка-ключ понадобится при переустановке приложения. "
            "Устанавливайте приложения только по ссылкам из письма: клон из поиска "
            "может украсть вашу ссылку-ключ.</p>"
        )

        await self._send_email(email=email, subject="Ваша VPN-подписка активирована", html=html)

    async def _send_email(self, email: str, subject: str, html: str) -> None:
        mailgun = self._get_mailgun_config()
        if mailgun is None:
            logger.warning(f"Mailgun is not configured, skipping landing email '{subject}'")
            return

        async with AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{mailgun.base_url}/v3/{mailgun.domain}/messages",
                auth=("api", mailgun.api_key.get_secret_value()),
                data={"from": mailgun.from_email, "to": email, "subject": subject, "html": html},
            )
            response.raise_for_status()

        logger.info(f"Landing email '{subject}' sent to {email}")

    def _get_mailgun_config(self) -> MailgunConfig | None:
        if self.config.mailgun is not None:
            return self.config.mailgun

        try:
            return MailgunConfig()
        except Exception:
            return None
