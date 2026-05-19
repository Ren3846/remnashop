import hmac
import re
import uuid
from datetime import UTC, datetime, timedelta

import orjson
from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from httpx import AsyncClient
from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis
from remnapy import RemnawaveSDK
from remnapy.models import CreateUserRequestDto
from remnapy.models.users import TrafficLimitStrategy

from src.application.common.dao import PaymentGatewayDao
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.core.config import AppConfig
from src.core.config.mailgun import MailgunConfig
from src.core.enums import PaymentGatewayType
from src.infrastructure.payment_gateways.lava import verify_lava_webhook

router = APIRouter(prefix="/checkout")

_WEB_PAYMENT_TTL = 60 * 60 * 24  # 24h


def _redis_key(payment_id: str) -> str:
    return f"web_payment:{payment_id}"


class CreateLavaLinkRequest(BaseModel):
    email: str
    days: int = 30
    offerId: str | None = None
    periodicity: str = "MONTHLY"


class CreateLavaLinkResponse(BaseModel):
    ok: bool = True
    provider: str = "lava"
    mode: str = "landing"
    days: int
    payUrl: str
    invoiceId: str
    orderId: str


def _check_secret(x_landing_secret: str | None, config: AppConfig) -> None:
    expected = config.landing_secret
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Landing not configured")
    if not x_landing_secret or not hmac.compare_digest(
        expected.get_secret_value(), x_landing_secret
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret")


@router.post("/lava/create-link-landing", response_model=CreateLavaLinkResponse)
@inject
async def create_lava_link_landing(
    body: CreateLavaLinkRequest,
    config: FromDishka[AppConfig],
    redis: FromDishka[Redis],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    x_landing_secret: str | None = Header(default=None),
) -> CreateLavaLinkResponse:
    _check_secret(x_landing_secret, config)

    gateway = await payment_gateway_dao.get_by_type(PaymentGatewayType.LAVA)
    if not gateway or not gateway.is_active or not isinstance(gateway.settings, LavaGatewaySettingsDto):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payment gateway not configured")

    settings: LavaGatewaySettingsDto = gateway.settings
    api_key = settings.api_key.get_secret_value()  # type: ignore[union-attr]
    offer_id = body.offerId or settings.offer_id

    payload = {
        "offerId": offer_id,
        "email": body.email,
        "currency": "RUB",
        "periodicity": body.periodicity,
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

    payment_id: str = data["id"]
    payment_url: str = data["paymentUrl"]
    order_id: str = data.get("orderId", payment_id)

    await redis.setex(
        _redis_key(payment_id),
        _WEB_PAYMENT_TTL,
        orjson.dumps({"email": body.email, "days": body.days}),
    )

    logger.info(f"Web payment created: id={payment_id}, email={body.email}, days={body.days}")

    return CreateLavaLinkResponse(
        days=body.days,
        payUrl=payment_url,
        invoiceId=payment_id,
        orderId=order_id,
    )


@router.post("/lava/webhook-landing")
@inject
async def lava_landing_webhook(
    request: Request,
    config: FromDishka[AppConfig],
    redis: FromDishka[Redis],
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    remnawave_sdk: FromDishka[RemnawaveSDK],
) -> Response:
    body = await request.body()

    gateway = await payment_gateway_dao.get_by_type(PaymentGatewayType.LAVA)
    if not gateway or not isinstance(gateway.settings, LavaGatewaySettingsDto):
        return Response(status_code=status.HTTP_200_OK)

    settings: LavaGatewaySettingsDto = gateway.settings
    secret = settings.webhook_secret.get_secret_value()  # type: ignore[union-attr]

    if not verify_lava_webhook(request, body, secret):
        logger.warning("Invalid Lava landing webhook signature")
        return Response(status_code=status.HTTP_200_OK)

    data = orjson.loads(body)
    event_type = data.get("eventType", "")
    contract_id = data.get("contractId")

    if not contract_id or event_type not in (
        "payment.success",
        "subscription.recurring.payment.success",
    ):
        return Response(status_code=status.HTTP_200_OK)

    raw = await redis.get(_redis_key(contract_id))
    if not raw:
        return Response(status_code=status.HTTP_200_OK)

    payment_data = orjson.loads(raw)
    email: str = payment_data["email"]
    days: int = payment_data.get("days", 30)
    await redis.delete(_redis_key(contract_id))

    username = (
        "web_"
        + re.sub(r"[^a-zA-Z0-9]", "_", email.split("@")[0])[:20]
        + "_"
        + uuid.uuid4().hex[:6]
    )

    try:
        remna_user = await remnawave_sdk.users.create_user(
            CreateUserRequestDto(
                username=username,
                expire_at=datetime.now(UTC) + timedelta(days=days),
                traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
            )
        )
        subscription_url = remna_user.subscription_url
        logger.info(f"Remnawave user '{username}' created for email={email}")
    except Exception as e:
        logger.error(f"Failed to create Remnawave user for email={email}: {e}")
        return Response(status_code=status.HTTP_200_OK)

    try:
        mailgun = MailgunConfig()
        async with AsyncClient(timeout=10) as client:
            await client.post(
                f"{mailgun.base_url}/v3/{mailgun.domain}/messages",
                auth=("api", mailgun.api_key.get_secret_value()),
                data={
                    "from": mailgun.from_email,
                    "to": email,
                    "subject": "Ваша VPN подписка активирована",
                    "html": (
                        "<h2>Спасибо за покупку!</h2>"
                        "<p>Нажмите на ссылку — приложение Happ откроется автоматически:</p>"
                        f"<p><a href='{subscription_url}'>{subscription_url}</a></p>"
                        f"<p>Срок действия: <b>{days} дней</b></p>"
                    ),
                },
            )
        logger.info(f"VPN link sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send email to {email}: {e}")

    return Response(status_code=status.HTTP_200_OK)
