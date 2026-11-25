import hashlib
import hmac
from decimal import Decimal
from typing import Any, Final, Optional
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


def verify_lava_webhook(request: Request, raw_body: bytes, secret: str) -> bool:
    signature = request.headers.get("signature")
    if signature:
        if hmac.compare_digest(signature, secret):
            return True

        expected_sha256 = hashlib.sha256(raw_body + secret.encode()).hexdigest()
        if hmac.compare_digest(expected_sha256, signature):
            return True

        expected_hmac = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected_hmac, signature):
            return True

        logger.warning("Invalid Lava.top webhook signature")
        return False

    api_key = request.headers.get("x-api-key")
    if api_key:
        if hmac.compare_digest(api_key, secret):
            return True

        logger.warning("Invalid Lava.top webhook X-Api-Key")
        return False

    logger.warning("Lava.top webhook missing 'signature' or 'X-Api-Key' header")
    return False


class LavaOfferNotFoundError(RuntimeError):
    pass


# Lava.top takes the price from the offer, not from the request, so the offer has to be
# picked by the amount we are about to show the buyer. ONE_TIME offers are preferred:
# recurring charges are not handled by the bot.
_PERIODICITY_ORDER: Final[tuple[str, ...]] = (
    "ONE_TIME",
    "MONTHLY",
    "PERIOD_90_DAYS",
    "PERIOD_180_DAYS",
    "PERIOD_YEAR",
)


def pick_lava_offer(products: list[dict[str, Any]], amount: Decimal) -> tuple[str, str]:
    matches: list[tuple[int, str, str]] = []
    for product in products:
        for offer in product.get("offers") or []:
            for price in offer.get("prices") or []:
                periodicity = price.get("periodicity") or "ONE_TIME"
                if price.get("currency") != "RUB" or periodicity not in _PERIODICITY_ORDER:
                    continue
                if Decimal(str(price.get("amount"))) == amount:
                    matches.append(
                        (_PERIODICITY_ORDER.index(periodicity), offer["id"], periodicity)
                    )

    if not matches:
        raise LavaOfferNotFoundError(f"No Lava.top offer priced at {amount} RUB")

    _, offer_id, periodicity = min(matches)
    return offer_id, periodicity


async def fetch_lava_products(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/api/v2/products", params={"showAllSubscriptionPeriods": "true"})
    response.raise_for_status()
    return orjson.loads(response.content).get("items") or []


async def resolve_lava_offer(client: AsyncClient, amount: Decimal) -> tuple[str, str]:
    return pick_lava_offer(await fetch_lava_products(client), amount)


# https://gate.lava.top/docs
class LavaGateway(BasePaymentGateway):
    _client: AsyncClient
    parent_payment_id: Optional[UUID] = None

    API_BASE: Final[str] = "https://gate.lava.top"

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(self.data.settings, LavaGatewaySettingsDto):
            raise TypeError(
                f"Invalid settings type: expected {LavaGatewaySettingsDto.__name__}, "
                f"got {type(self.data.settings).__name__}"
            )

        settings: LavaGatewaySettingsDto = self.data.settings
        api_key = settings.api_key.get_secret_value()  # type: ignore[union-attr]

        self._client = self._make_client(
            base_url=self.API_BASE,
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        )

    async def handle_create_payment(
        self, amount: Decimal, details: str, email: Optional[str] = None
    ) -> PaymentResultDto:
        offer_id, periodicity = await resolve_lava_offer(self._client, amount)

        payload = {
            "offerId": offer_id,
            "email": email or "noreply@example.com",
            "currency": "RUB",
            "periodicity": periodicity,
            "buyerLanguage": "RU",
        }

        body = orjson.dumps(payload)
        logger.debug(f"Creating Lava.top payment, amount={amount}, payload={payload}")

        try:
            response = await self._client.post("/api/v2/invoice", content=body)
            if response.status_code >= 400:
                logger.error(f"Lava.top API error {response.status_code}: {response.text}")
            response.raise_for_status()
            data = orjson.loads(response.content)

            payment_id = UUID(data["id"])
            payment_url = data["paymentUrl"]

            return PaymentResultDto(id=payment_id, url=payment_url)

        except HTTPStatusError as e:
            logger.error(
                f"HTTP error creating Lava.top payment. "
                f"Status: '{e.response.status_code}', Body: {e.response.text}"
            )
            raise
        except (KeyError, orjson.JSONDecodeError) as e:
            logger.error(f"Failed to parse Lava.top response. Error: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error creating Lava.top payment: {e}")
            raise

    async def handle_webhook(self, request: Request) -> Optional[tuple[UUID, TransactionStatus]]:
        logger.debug("Received Lava.top webhook request")

        body = await request.body()

        if not self._verify_webhook(request, body):
            raise PermissionError("Webhook verification failed")

        webhook_data = orjson.loads(body)
        contract_id = webhook_data.get("contractId")

        if not contract_id:
            raise ValueError("Required field 'contractId' is missing in Lava.top webhook")

        event_type = webhook_data.get("eventType", "")
        payment_id = UUID(contract_id)
        # parentContractId is set on recurring charges; logged to trace renewals.
        logger.info(
            f"Lava.top webhook: event='{event_type}', contract='{contract_id}', "
            f"parent='{webhook_data.get('parentContractId')}'"
        )

        # Set only for a recurring charge: the id of the first contract of that subscription.
        self.parent_payment_id = None

        match event_type:
            case "payment.success":
                transaction_status = TransactionStatus.COMPLETED
            case "subscription.recurring.payment.success":
                parent_contract_id = webhook_data.get("parentContractId")
                if not parent_contract_id:
                    raise ValueError("Lava.top recurring payment without 'parentContractId'")
                self.parent_payment_id = UUID(parent_contract_id)
                transaction_status = TransactionStatus.COMPLETED
            case "payment.failed":
                transaction_status = TransactionStatus.CANCELED
            case "subscription.recurring.payment.failed" | "subscription.cancelled":
                # Nothing to undo: access simply runs out at the paid date.
                logger.info(f"Lava.top '{event_type}' for contract '{contract_id}' ignored")
                return None
            case _:
                raise ValueError(f"Unsupported Lava.top event type: '{event_type}'")

        return payment_id, transaction_status

    def _verify_webhook(self, request: Request, raw_body: bytes) -> bool:
        settings: LavaGatewaySettingsDto = self.data.settings  # type: ignore[assignment]
        secret = settings.webhook_secret.get_secret_value()  # type: ignore[union-attr]
        return verify_lava_webhook(request, raw_body, secret)
