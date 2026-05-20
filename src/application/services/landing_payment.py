import re
import uuid
from typing import Any, Optional
from uuid import UUID

import orjson
from adaptix import Retort
from httpx import AsyncClient
from loguru import logger
from redis.asyncio import Redis
from remnapy import RemnawaveSDK
from remnapy.models import CreateUserRequestDto

from src.application.common.dao import PaymentGatewayDao, PlanDao
from src.application.dto import PlanSnapshotDto
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.core.config import AppConfig
from src.core.config.mailgun import MailgunConfig
from src.core.enums import PaymentGatewayType, PlanType
from src.core.utils.converters import days_to_datetime, gb_to_bytes

_REDIS_PREFIX = "web_payment:"
_REDIS_TTL = 60 * 60 * 24


def landing_redis_key(payment_id: UUID | str) -> str:
    return f"{_REDIS_PREFIX}{payment_id}"


class LandingPaymentService:
    def __init__(
        self,
        config: AppConfig,
        redis: Redis,
        payment_gateway_dao: PaymentGatewayDao,
        plan_dao: PlanDao,
        remnawave_sdk: RemnawaveSDK,
        retort: Retort,
    ) -> None:
        self.config = config
        self.redis = redis
        self.payment_gateway_dao = payment_gateway_dao
        self.plan_dao = plan_dao
        self.remnawave_sdk = remnawave_sdk
        self.retort = retort

    async def create_lava_payment_link(
        self,
        email: str,
        days: int,
        offer_id: Optional[str] = None,
        periodicity: str = "ONE_TIME",
    ) -> dict[str, Any]:
        gateway = await self.payment_gateway_dao.get_by_type(PaymentGatewayType.LAVA)
        if not gateway or not gateway.is_active or not isinstance(
            gateway.settings, LavaGatewaySettingsDto
        ):
            raise RuntimeError("Lava payment gateway is not configured")

        settings: LavaGatewaySettingsDto = gateway.settings
        api_key = settings.api_key.get_secret_value()  # type: ignore[union-attr]
        resolved_offer_id = offer_id or settings.offer_id

        plan_snapshot = await self._resolve_plan_snapshot(days)

        payload = {
            "offerId": resolved_offer_id,
            "email": email,
            "currency": "RUB",
            "periodicity": periodicity,
            "buyerLanguage": "RU",
        }

        async with AsyncClient(
            base_url="https://gate.lava.top",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=15,
        ) as client:
            response = await client.post("/api/v2/invoice", content=orjson.dumps(payload))
            response.raise_for_status()
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
                    "periodicity": periodicity,
                    "plan_snapshot": self.retort.dump(plan_snapshot),
                }
            ),
        )

        logger.info(
            f"Landing payment created: id={payment_id}, email={email}, days={days}, "
            f"offer={resolved_offer_id}"
        )

        return {
            "days": days,
            "payUrl": payment_url,
            "invoiceId": payment_id,
            "orderId": order_id,
        }

    async def try_fulfill(self, payment_id: UUID) -> bool:
        raw = await self.redis.get(landing_redis_key(payment_id))
        if not raw:
            return False

        payment_data = orjson.loads(raw)
        email: str = payment_data["email"]
        days: int = payment_data.get("days", 30)
        plan_data = payment_data.get("plan_snapshot")

        await self.redis.delete(landing_redis_key(payment_id))

        plan_snapshot = (
            self.retort.load(plan_data, PlanSnapshotDto)
            if plan_data
            else self._default_plan_snapshot(days)
        )
        plan_snapshot.duration = days
        username = self._build_username(email)

        try:
            request = self._build_create_request(email=email, username=username, plan=plan_snapshot)
            remna_user = await self.remnawave_sdk.users.create_user(request)
            subscription_url = remna_user.subscription_url
            logger.info(f"Landing Remnawave user '{username}' created for email={email}")
        except Exception as e:
            logger.exception(f"Failed to create Remnawave user for landing email={email}: {e}")
            raise

        await self._send_activation_email(email=email, subscription_url=subscription_url, days=days)
        return True

    async def _resolve_plan_snapshot(self, days: int) -> PlanSnapshotDto:
        for plan in await self.plan_dao.get_active_allowed_plans():
            if plan.is_trial:
                continue
            if plan.get_duration(days):
                return PlanSnapshotDto.from_plan(plan, days)

        logger.warning(f"No active plan found for landing duration={days}, using defaults")
        return PlanSnapshotDto(
            id=-1,
            name="landing",
            type=PlanType.BOTH,
            traffic_limit=100,
            device_limit=1,
            duration=days,
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
    ) -> CreateUserRequestDto:
        return CreateUserRequestDto(
            username=username,
            email=email,
            expire_at=days_to_datetime(plan.duration),
            traffic_limit_strategy=plan.traffic_limit_strategy,
            traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
            hwid_device_limit=plan.device_limit,
            tag=plan.tag,
            active_internal_squads=plan.internal_squads,
            external_squad_uuid=plan.external_squad,
        )

    async def _send_activation_email(self, email: str, subscription_url: str, days: int) -> None:
        mailgun = self._get_mailgun_config()
        if mailgun is None:
            logger.warning("Mailgun is not configured, skipping landing activation email")
            return

        async with AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{mailgun.base_url}/v3/{mailgun.domain}/messages",
                auth=("api", mailgun.api_key.get_secret_value()),
                data={
                    "from": mailgun.from_email,
                    "to": email,
                    "subject": "VEPEN VPN — подписка активирована",
                    "html": (
                        "<h2>Спасибо за покупку!</h2>"
                        "<p>Ваша VPN-подписка активирована.</p>"
                        "<p>Нажмите на ссылку — приложение откроется автоматически:</p>"
                        f"<p><a href='{subscription_url}'>{subscription_url}</a></p>"
                        f"<p>Срок действия: <b>{days} дней</b></p>"
                        "<p>Также вы можете открыть Telegram-бот "
                        "<a href='https://t.me/vepen_bot'>@vepen_bot</a>.</p>"
                    ),
                },
            )
            response.raise_for_status()

        logger.info(f"Landing activation email sent to {email}")

    def _get_mailgun_config(self) -> MailgunConfig | None:
        if self.config.mailgun is not None:
            return self.config.mailgun

        try:
            return MailgunConfig()
        except Exception:
            return None
