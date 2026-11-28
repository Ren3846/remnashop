from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram_dialog import StartMode

from src.telegram.routers.common.email_handlers import _continue_after_email
from src.telegram.states import MainMenu, Subscription


def _manager(state):  # type: ignore[no-untyped-def]
    manager = MagicMock()
    manager.current_context.return_value = SimpleNamespace(state=state)
    manager.switch_to = AsyncMock()
    manager.start = AsyncMock()
    return manager


async def test_trial_from_the_main_menu_is_granted_right_away() -> None:
    # Regression: this used to switch_to(Subscription.MAIN) from the main menu dialog,
    # which aiogram_dialog refuses ("Cannot switch to another state group").
    manager = _manager(MainMenu.LINK_EMAIL_CONFIRM)
    with patch(
        "src.telegram.routers.menu.handlers.continue_trial_after_email", new=AsyncMock()
    ) as grant:
        await _continue_after_email(manager, "trial")
    grant.assert_awaited_once_with(manager)
    manager.switch_to.assert_not_called()


async def test_subscription_from_the_main_menu_starts_the_other_dialog() -> None:
    manager = _manager(MainMenu.LINK_EMAIL_CONFIRM)
    await _continue_after_email(manager, "subscription")
    manager.start.assert_awaited_once_with(Subscription.MAIN)
    manager.switch_to.assert_not_called()


async def test_inside_the_subscription_dialog_it_stays_there() -> None:
    manager = _manager(Subscription.LINK_EMAIL_CONFIRM)
    await _continue_after_email(manager, "subscription")
    manager.switch_to.assert_awaited_once_with(Subscription.MAIN)


async def test_menu_target_from_the_subscription_dialog_restarts_the_menu() -> None:
    manager = _manager(Subscription.LINK_EMAIL_CONFIRM)
    await _continue_after_email(manager, "menu")
    manager.start.assert_awaited_once_with(MainMenu.MAIN, mode=StartMode.RESET_STACK)


async def test_menu_target_inside_the_menu_switches() -> None:
    manager = _manager(MainMenu.LINK_EMAIL_CONFIRM)
    await _continue_after_email(manager, None)
    manager.switch_to.assert_awaited_once_with(MainMenu.MAIN)
