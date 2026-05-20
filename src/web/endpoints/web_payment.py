import hmac

import orjson
from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from uuid import UUID

from src.application.common.dao import PaymentGatewayDao
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.application.services.landing_payment import LandingPaymentService
from src.core.config import AppConfig
from src.core.enums import PaymentGatewayType
from src.infrastructure.payment_gateways.lava import verify_lava_webhook

router = APIRouter(prefix="/checkout")


class CreateLavaLinkRequest(BaseModel):
    email: EmailStr
    days: int = Field(default=30, ge=1)
    offerId: str | None = None
    periodicity: str = "ONE_TIME"


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
    landing_payment: FromDishka[LandingPaymentService],
    x_landing_secret: str | None = Header(default=None),
) -> CreateLavaLinkResponse:
    _check_secret(x_landing_secret, config)

    try:
        result = await landing_payment.create_lava_payment_link(
            email=str(body.email),
            days=body.days,
            offer_id=body.offerId,
            periodicity=body.periodicity,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e

    return CreateLavaLinkResponse(**result)


@router.post("/lava/webhook-landing")
@inject
async def lava_landing_webhook(
    request: Request,
    payment_gateway_dao: FromDishka[PaymentGatewayDao],
    landing_payment: FromDishka[LandingPaymentService],
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

    try:
        if await landing_payment.try_fulfill(UUID(contract_id)):
            logger.info(f"Landing webhook fulfilled payment '{contract_id}'")
    except Exception as e:
        logger.exception(f"Landing webhook failed for payment '{contract_id}': {e}")

    return Response(status_code=status.HTTP_200_OK)
