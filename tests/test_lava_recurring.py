import asyncio
from types import SimpleNamespace
from uuid import uuid4

import orjson
import pytest

from src.application.use_cases.gateways.commands.payment import ProcessPayment
from src.core.enums import PurchaseType, TransactionStatus
from src.infrastructure.payment_gateways.lava import LavaGateway


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._body = orjson.dumps(payload)

    async def body(self) -> bytes:
        return self._body


def webhook(payload: dict):
    gateway = object.__new__(LavaGateway)
    gateway._verify_webhook = lambda request, body: True
    return gateway, asyncio.run(gateway.handle_webhook(FakeRequest(payload)))


def test_recurring_success_carries_the_first_contract() -> None:
    contract, parent = uuid4(), uuid4()
    gateway, result = webhook(
        {
            "eventType": "subscription.recurring.payment.success",
            "contractId": str(contract),
            "parentContractId": str(parent),
        }
    )
    assert result == (contract, TransactionStatus.COMPLETED)
    assert gateway.parent_payment_id == parent


def test_first_payment_has_no_parent() -> None:
    contract = uuid4()
    gateway, result = webhook({"eventType": "payment.success", "contractId": str(contract)})
    assert result == (contract, TransactionStatus.COMPLETED)
    assert gateway.parent_payment_id is None


@pytest.mark.parametrize(
    "event", ["subscription.recurring.payment.failed", "subscription.cancelled"]
)
def test_failed_renewal_and_cancellation_change_nothing(event: str) -> None:
    _, result = webhook({"eventType": event, "contractId": str(uuid4())})
    assert result is None


class FakeUow:
    committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1


class FakeTransactions:
    def __init__(self, existing: dict) -> None:
        self.rows = dict(existing)

    async def exists(self, payment_id) -> bool:
        return payment_id in self.rows

    async def get_by_payment_id(self, payment_id):
        return self.rows.get(payment_id)

    async def create(self, transaction):
        self.rows[transaction.payment_id] = transaction
        return transaction


def process_payment(transactions: FakeTransactions, has_subscription: bool) -> ProcessPayment:
    interactor = object.__new__(ProcessPayment)
    interactor.uow = FakeUow()
    interactor.transaction_dao = transactions

    async def get_current(user_id: int):
        return SimpleNamespace(id=1) if has_subscription else None

    interactor.subscription_dao = SimpleNamespace(get_current=get_current)
    return interactor


def parent_transaction(payment_id):
    return SimpleNamespace(
        payment_id=payment_id,
        user_id=7,
        gateway_type="LAVA",
        gateway_display_name=None,
        pricing="pricing",
        currency="RUB",
        plan_snapshot="plan",
    )


def test_recurring_charge_opens_a_renewal_for_the_same_user_and_plan() -> None:
    parent_id, payment_id = uuid4(), uuid4()
    transactions = FakeTransactions({parent_id: parent_transaction(parent_id)})
    interactor = process_payment(transactions, has_subscription=True)

    asyncio.run(interactor._open_recurring_transaction(payment_id, parent_id))
    # A repeated webhook must not open a second transaction.
    asyncio.run(interactor._open_recurring_transaction(payment_id, parent_id))

    opened = transactions.rows[payment_id]
    assert (opened.user_id, opened.plan_snapshot, opened.pricing) == (7, "plan", "pricing")
    assert opened.status == TransactionStatus.PENDING
    assert opened.purchase_type == PurchaseType.RENEW
    assert interactor.uow.committed == 1


def test_recurring_charge_without_a_subscription_buys_a_new_one() -> None:
    parent_id, payment_id = uuid4(), uuid4()
    transactions = FakeTransactions({parent_id: parent_transaction(parent_id)})
    interactor = process_payment(transactions, has_subscription=False)
    asyncio.run(interactor._open_recurring_transaction(payment_id, parent_id))
    assert transactions.rows[payment_id].purchase_type == PurchaseType.NEW


def test_recurring_charge_with_unknown_first_payment_opens_nothing() -> None:
    transactions = FakeTransactions({})
    interactor = process_payment(transactions, has_subscription=True)
    payment_id = uuid4()
    asyncio.run(interactor._open_recurring_transaction(payment_id, uuid4()))
    assert payment_id not in transactions.rows
