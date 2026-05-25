from dataclasses import dataclass

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import SiteDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import UserDto
from src.application.dto.site import SiteDto, SiteKeywordDto, SiteSubdomainDto
from src.core.utils.site import is_valid_domain, normalize_domain, normalize_subdomain_host


@dataclass
class CreateSiteDto:
    name: str
    base_domain: str


@dataclass
class CreateSubdomainDto:
    site_id: int
    host: str


@dataclass
class AddSiteKeywordDto:
    subdomain_id: int
    keyword: str


class CreateSite(Interactor[CreateSiteDto, SiteDto]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, data: CreateSiteDto) -> SiteDto:
        name = data.name.strip()
        base_domain = normalize_domain(data.base_domain)

        if not name:
            raise ValueError("Site name is required")
        if not is_valid_domain(base_domain):
            raise ValueError("Invalid base domain")

        async with self.uow:
            existing = await self.site_dao.get_site_by_base_domain(base_domain)
            if existing:
                raise ValueError("Site with this base domain already exists")

            site = await self.site_dao.create_site(
                SiteDto(name=name, base_domain=base_domain, is_active=True)
            )
            await self.uow.commit()

        logger.info(f"{actor.log} Created site '{base_domain}'")
        return site


class CreateSubdomain(Interactor[CreateSubdomainDto, SiteSubdomainDto]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, data: CreateSubdomainDto) -> SiteSubdomainDto:
        async with self.uow:
            site = await self.site_dao.get_site_by_id(data.site_id)
            if not site:
                raise ValueError("Site not found")

            host = normalize_subdomain_host(data.host, site.base_domain)
            if not is_valid_domain(host):
                raise ValueError("Invalid subdomain host")

            existing = await self.site_dao.get_subdomain_by_host(host)
            if existing:
                raise ValueError("Subdomain already exists")

            subdomain = await self.site_dao.create_subdomain(
                SiteSubdomainDto(site_id=site.id, host=host, is_active=True)  # type: ignore[arg-type]
            )
            await self.uow.commit()

        logger.info(f"{actor.log} Created subdomain '{host}'")
        return subdomain


class AddSiteKeyword(Interactor[AddSiteKeywordDto, SiteKeywordDto]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, data: AddSiteKeywordDto) -> SiteKeywordDto:
        keyword = " ".join(data.keyword.strip().split())
        if not keyword:
            raise ValueError("Keyword is required")

        async with self.uow:
            subdomain = await self.site_dao.get_subdomain_by_id(data.subdomain_id)
            if not subdomain:
                raise ValueError("Subdomain not found")

            existing = await self.site_dao.get_keywords_by_subdomain_id(subdomain.id)  # type: ignore[arg-type]
            if any(item.keyword.lower() == keyword.lower() for item in existing):
                raise ValueError("Keyword already exists for this subdomain")

            created = await self.site_dao.create_keyword(
                SiteKeywordDto(
                    subdomain_id=subdomain.id,  # type: ignore[arg-type]
                    keyword=keyword,
                    is_active=True,
                )
            )
            await self.uow.commit()

        logger.info(f"{actor.log} Added keyword '{keyword}' to subdomain '{subdomain.host}'")
        return created


class ToggleSiteActive(Interactor[int, None]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, site_id: int) -> None:
        async with self.uow:
            site = await self.site_dao.get_site_by_id(site_id)
            if not site:
                raise ValueError("Site not found")

            site.is_active = not site.is_active
            await self.site_dao.update_site(site)
            await self.uow.commit()

        logger.info(f"{actor.log} Toggled site '{site.base_domain}' active={site.is_active}")


class ToggleSubdomainActive(Interactor[int, None]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, subdomain_id: int) -> None:
        async with self.uow:
            subdomain = await self.site_dao.get_subdomain_by_id(subdomain_id)
            if not subdomain:
                raise ValueError("Subdomain not found")

            subdomain.is_active = not subdomain.is_active
            await self.site_dao.update_subdomain(subdomain)
            await self.uow.commit()

        logger.info(f"{actor.log} Toggled subdomain '{subdomain.host}' active={subdomain.is_active}")


class ToggleSiteKeywordActive(Interactor[int, None]):
    required_permission = Permission.MANAGE_SITES

    def __init__(self, uow: UnitOfWork, site_dao: SiteDao) -> None:
        self.uow = uow
        self.site_dao = site_dao

    async def _execute(self, actor: UserDto, keyword_id: int) -> None:
        async with self.uow:
            keyword = await self.site_dao.get_keyword_by_id(keyword_id)
            if not keyword:
                raise ValueError("Keyword not found")

            keyword.is_active = not keyword.is_active
            await self.site_dao.update_keyword(keyword)
            await self.uow.commit()

        logger.info(f"{actor.log} Toggled keyword '{keyword.keyword}' active={keyword.is_active}")
