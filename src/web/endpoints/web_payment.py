import hmac
from collections.abc import AsyncIterator
from uuid import UUID

import orjson
from dishka import AsyncContainer
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from loguru import logger
from pydantic import BaseModel, EmailStr, Field

from src.application.common.dao import PaymentGatewayDao
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.application.services.landing_payment import LandingPaymentService
from src.core.config import AppConfig
from src.core.enums import PaymentGatewayType
from src.infrastructure.payment_gateways.lava import verify_lava_webhook
from src.web.dishka_context import with_request_container

router = APIRouter(prefix="/checkout")


class CreateLavaLinkRequest(BaseModel):
    email: EmailStr
    days: int = Field(default=30, ge=1)
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
async def create_lava_link_landing(
    body: CreateLavaLinkRequest,
    x_landing_secret: str | None = Header(default=None),
    request_container: AsyncContainer = Depends(with_request_container),
) -> CreateLavaLinkResponse:
    config = AppConfig.get()
    _check_secret(x_landing_secret, config)

    try:
        landing_payment = await request_container.get(LandingPaymentService)
        result = await landing_payment.create_lava_payment_link(
            email=str(body.email),
            days=body.days,
            offer_id=body.offerId,
            periodicity=body.periodicity,
        )
    except RuntimeError as e:
        message = str(e)
        status_code = (
            status.HTTP_502_BAD_GATEWAY
            if message.startswith("Lava payment failed")
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(status_code=status_code, detail=message) from e
    except Exception as e:
        logger.exception(
            f"Failed to create landing payment link for email={body.email}, days={body.days}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create payment link",
        ) from e

    return CreateLavaLinkResponse(**result)


@router.post("/lava/webhook-landing")
async def lava_landing_webhook(
    request: Request,
    request_container: AsyncContainer = Depends(with_request_container),
) -> Response:
    body = await request.body()

    payment_gateway_dao = await request_container.get(PaymentGatewayDao)
    landing_payment = await request_container.get(LandingPaymentService)

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
