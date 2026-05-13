import hashlib
import hmac
import uuid
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import orjson
from aiogram import Bot
from fastapi import Request
from httpx import AsyncClient, HTTPStatusError
from loguru import logger

from src.application.dto import PaymentGatewayDto, PaymentResultDto
from src.application.dto.payment_gateway import LavaGatewaySettingsDto
from src.core.config import AppConfig
from src.core.enums import TransactionStatus

from .base import BasePaymentGateway


# https://dev.lava.ru/
class LavaGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE: Final[str] = "https://api.lava.ru/business"
    INVOICE_EXPIRE_MINUTES: Final[int] = 30

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(self.data.settings, LavaGatewaySettingsDto):
            raise TypeError(
                f"Invalid settings type: expected {LavaGatewaySettingsDto.__name__}, "
                f"got {type(self.data.settings).__name__}"
            )

        self._client = self._make_client(base_url=self.API_BASE)

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        order_id = uuid.uuid4()
        payload = await self._create_payment_payload(str(order_id), str(amount), details)
        body = orjson.dumps(payload)
        signature = self._sign(body)
        logger.debug(f"Creating Lava payment, order_id={order_id}")

        try:
            response = await self._client.post(
                "/invoice/create",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Signature": signature,
                },
            )
            response.raise_for_status()
            data = orjson.loads(response.content)

            if data.get("status") != 200:
                raise ValueError(f"Lava API error: {data}")

            return self._get_payment_data(order_id, data.get("data", {}))

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error creating Lava payment. "
                f"Status: '{e.response.status_code}', Body: {e.response.text}"
            )
            raise
        except (KeyError, orjson.JSONDecodeError) as e:
            logger.error(f"Failed to parse Lava response. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error creating Lava payment: {e}")
            raise

    async def handle_webhook(self, request: Request) -> tuple[UUID, TransactionStatus]:
        logger.debug("Received Lava webhook request")

        body = await request.body()

        if not self._verify_webhook(request, body):
            raise PermissionError("Webhook verification failed")

        webhook_data = orjson.loads(body)
        order_id_str = webhook_data.get("order_id")

        if not order_id_str:
            raise ValueError("Required field 'order_id' is missing in Lava webhook")

        status = webhook_data.get("status")
        payment_id = UUID(order_id_str)

        match status:
            case "success":
                transaction_status = TransactionStatus.COMPLETED
            case "error" | "canceled":
                transaction_status = TransactionStatus.CANCELED
            case _:
                raise ValueError(f"Unsupported Lava webhook status: '{status}'")

        return payment_id, transaction_status

    async def _create_payment_payload(
        self, order_id: str, amount: str, details: str
    ) -> dict[str, Any]:
        bot_url = await self._get_bot_redirect_url()
        webhook_url = self._get_webhook_url()
        return {
            "orderId": order_id,
            "sum": float(amount),
            "shopId": self.data.settings.shop_id,  # type: ignore[union-attr]
            "hookUrl": webhook_url,
            "successUrl": bot_url,
            "failUrl": bot_url,
            "expire": self.INVOICE_EXPIRE_MINUTES,
            "comment": details,
        }

    def _get_payment_data(self, order_id: UUID, data: dict[str, Any]) -> PaymentResultDto:
        payment_url = data.get("url")
        if not payment_url:
            raise KeyError("Invalid response from Lava API: missing 'url'")
        return PaymentResultDto(id=order_id, url=str(payment_url))

    def _sign(self, body: bytes) -> str:
        secret = self.data.settings.secret_key.get_secret_value()  # type: ignore[union-attr]
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def _verify_webhook(self, request: Request, raw_body: bytes) -> bool:
        signature = request.headers.get("X-Signature")
        if not signature:
            logger.warning("Lava webhook missing 'X-Signature' header")
            return False

        expected = self._sign(raw_body)
        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Lava webhook signature")
            return False

        return True

    def _get_webhook_url(self) -> str:
        from src.core.enums import PaymentGatewayType

        return self.config.get_webhook(PaymentGatewayType.LAVA)
