from typing import Any

from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common.dao import SiteDao


@inject
async def sites_getter(
    dialog_manager: DialogManager,
    site_dao: FromDishka[SiteDao],
    **kwargs: Any,
) -> dict[str, Any]:
    sites = await site_dao.get_all_sites()
    return {
        "sites": [
            {
                "id": site.id,
                "name": site.name,
                "base_domain": site.base_domain,
                "is_active": site.is_active,
                "title": f"{'🟢' if site.is_active else '🔴'} {site.name} ({site.base_domain})",
            }
            for site in sites
        ]
    }


@inject
async def site_getter(
    dialog_manager: DialogManager,
    site_dao: FromDishka[SiteDao],
    **kwargs: Any,
) -> dict[str, Any]:
    site_id = dialog_manager.dialog_data["site_id"]
    site = await site_dao.get_site_by_id(site_id)
    if not site:
        raise ValueError(f"Site '{site_id}' not found")

    subdomains = await site_dao.get_subdomains_by_site_id(site_id)
    return {
        "site_id": site.id,
        "name": site.name,
        "base_domain": site.base_domain,
        "is_active": site.is_active,
        "subdomains": [
            {
                "id": item.id,
                "host": item.host,
                "is_active": item.is_active,
                "title": f"{'🟢' if item.is_active else '🔴'} {item.host}",
            }
            for item in subdomains
        ],
    }


@inject
async def subdomain_getter(
    dialog_manager: DialogManager,
    site_dao: FromDishka[SiteDao],
    **kwargs: Any,
) -> dict[str, Any]:
    subdomain_id = dialog_manager.dialog_data["subdomain_id"]
    subdomain = await site_dao.get_subdomain_by_id(subdomain_id)
    if not subdomain:
        raise ValueError(f"Subdomain '{subdomain_id}' not found")

    site = await site_dao.get_site_by_id(subdomain.site_id)
    keywords = await site_dao.get_keywords_by_subdomain_id(subdomain_id)
    return {
        "subdomain_id": subdomain.id,
        "host": subdomain.host,
        "is_active": subdomain.is_active,
        "site_name": site.name if site else "",
        "keywords": [
            {
                "id": item.id,
                "keyword": item.keyword,
                "is_active": item.is_active,
                "title": f"{'🟢' if item.is_active else '🔴'} {item.keyword}",
            }
            for item in keywords
        ],
    }
