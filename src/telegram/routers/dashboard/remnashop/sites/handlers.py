from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.dto import UserDto
from src.application.use_cases.site import (
    AddSiteKeyword,
    AddSiteKeywordDto,
    CreateSite,
    CreateSiteDto,
    CreateSubdomain,
    CreateSubdomainDto,
    ToggleSiteActive,
    ToggleSiteKeywordActive,
    ToggleSubdomainActive,
)
from src.core.constants import USER_KEY
from src.telegram.states import RemnashopSites


@inject
async def on_site_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    dialog_manager.dialog_data["site_id"] = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    await dialog_manager.switch_to(state=RemnashopSites.SITE)


@inject
async def on_subdomain_select(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
) -> None:
    dialog_manager.dialog_data["subdomain_id"] = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    await dialog_manager.switch_to(state=RemnashopSites.SUBDOMAIN)


@inject
async def on_site_name_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
) -> None:
    dialog_manager.dialog_data["site_name"] = message.text or ""
    await dialog_manager.switch_to(state=RemnashopSites.SITE_DOMAIN)


@inject
async def on_site_domain_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    create_site: FromDishka[CreateSite],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    name = dialog_manager.dialog_data.get("site_name", "")

    try:
        site = await create_site(user, CreateSiteDto(name=name, base_domain=message.text or ""))
    except ValueError as e:
        await message.answer(str(e))
        return

    dialog_manager.dialog_data["site_id"] = site.id
    await dialog_manager.switch_to(state=RemnashopSites.SITE)


@inject
async def on_subdomain_host_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    create_subdomain: FromDishka[CreateSubdomain],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    site_id = dialog_manager.dialog_data["site_id"]

    try:
        subdomain = await create_subdomain(
            user,
            CreateSubdomainDto(site_id=site_id, host=message.text or ""),
        )
    except ValueError as e:
        await message.answer(str(e))
        return

    dialog_manager.dialog_data["subdomain_id"] = subdomain.id
    await dialog_manager.switch_to(state=RemnashopSites.SUBDOMAIN)


@inject
async def on_keyword_input(
    message: Message,
    widget: MessageInput,
    dialog_manager: DialogManager,
    add_site_keyword: FromDishka[AddSiteKeyword],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    subdomain_id = dialog_manager.dialog_data["subdomain_id"]

    try:
        await add_site_keyword(
            user,
            AddSiteKeywordDto(subdomain_id=subdomain_id, keyword=message.text or ""),
        )
    except ValueError as e:
        await message.answer(str(e))
        return

    await dialog_manager.switch_to(state=RemnashopSites.SUBDOMAIN)


@inject
async def on_site_active_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_site_active: FromDishka[ToggleSiteActive],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    site_id = dialog_manager.dialog_data["site_id"]
    await toggle_site_active(user, site_id)


@inject
async def on_subdomain_active_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_subdomain_active: FromDishka[ToggleSubdomainActive],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    subdomain_id = dialog_manager.dialog_data["subdomain_id"]
    await toggle_subdomain_active(user, subdomain_id)


@inject
async def on_keyword_active_toggle(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    toggle_site_keyword_active: FromDishka[ToggleSiteKeywordActive],
) -> None:
    user: UserDto = dialog_manager.middleware_data[USER_KEY]
    keyword_id = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    await toggle_site_keyword_active(user, keyword_id)
