from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.core.constants import WELCOME_CTA, WELCOME_TRIAL
from src.telegram.keyboards import get_welcome_keyboard
from src.telegram.routers.menu.handlers import _welcome_trial_text


def _rows(markup):  # type: ignore[no-untyped-def]
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def test_welcome_has_only_buy_without_a_trial() -> None:
    assert _rows(get_welcome_keyboard()) == [[WELCOME_CTA]]


def test_trial_button_goes_first() -> None:
    markup = get_welcome_keyboard("🎁 Trial")
    assert _rows(markup) == [[WELCOME_TRIAL], [WELCOME_CTA]]
    assert markup.inline_keyboard[0][0].text == "🎁 Trial"


def _deps(price: Decimal | None, plan_exists: bool = True):  # type: ignore[no-untyped-def]
    duration = SimpleNamespace(get_price=lambda currency: price)
    plan = SimpleNamespace(durations=[duration]) if plan_exists else None
    get_trial = SimpleNamespace(system=AsyncMock(return_value=plan))
    settings = SimpleNamespace(default_currency=SimpleNamespace(symbol="₽"))
    settings_dao = SimpleNamespace(get=AsyncMock(return_value=settings))
    i18n = MagicMock()
    i18n.get.side_effect = lambda key, **kw: f"{key}|{kw.get('trial_price')}"
    user = SimpleNamespace(log="[user]")
    return user, i18n, get_trial, settings_dao


async def test_free_trial_uses_the_menu_label_key() -> None:
    assert await _welcome_trial_text(*_deps(Decimal(0))) == "btn-menu.trial"


async def test_paid_trial_shows_the_price() -> None:
    assert await _welcome_trial_text(*_deps(Decimal("20.00"))) == "btn-menu.trial-paid|20 ₽"


async def test_no_trial_means_no_button() -> None:
    assert await _welcome_trial_text(*_deps(None, plan_exists=False)) is None


async def test_a_failure_never_breaks_the_welcome() -> None:
    user, i18n, get_trial, settings_dao = _deps(Decimal(0))
    get_trial.system.side_effect = RuntimeError("db down")
    assert await _welcome_trial_text(user, i18n, get_trial, settings_dao) is None
