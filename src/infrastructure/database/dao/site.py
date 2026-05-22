from typing import Optional, cast

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common.dao import SiteDao
from src.application.dto.site import SiteDto, SiteKeywordDto, SiteSubdomainDto
from src.infrastructure.database.models.site import Site, SiteKeyword, SiteSubdomain

from .base import BaseDaoImpl


class SiteDaoImpl(SiteDao, BaseDaoImpl):
    def __init__(self, session: AsyncSession, retort) -> None:
        super().__init__(session, retort)

    @staticmethod
    def _to_site_dto(model: Site) -> SiteDto:
        return SiteDto(
            id=model.id,
            name=model.name,
            base_domain=model.base_domain,
            is_active=model.is_active,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_subdomain_dto(model: SiteSubdomain) -> SiteSubdomainDto:
        return SiteSubdomainDto(
            id=model.id,
            site_id=model.site_id,
            host=model.host,
            is_active=model.is_active,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_keyword_dto(model: SiteKeyword) -> SiteKeywordDto:
        return SiteKeywordDto(
            id=model.id,
            subdomain_id=model.subdomain_id,
            keyword=model.keyword,
            is_active=model.is_active,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def create_site(self, site: SiteDto) -> SiteDto:
        db_site = Site(
            name=site.name,
            base_domain=site.base_domain,
            is_active=site.is_active,
        )
        self.session.add(db_site)
        await self.session.flush()
        logger.debug(f"Created site '{site.base_domain}'")
        return self._to_site_dto(db_site)

    async def get_site_by_id(self, site_id: int) -> Optional[SiteDto]:
        db_site = await self.session.scalar(select(Site).where(Site.id == site_id))
        return self._to_site_dto(db_site) if db_site else None

    async def get_site_by_base_domain(self, base_domain: str) -> Optional[SiteDto]:
        db_site = await self.session.scalar(select(Site).where(Site.base_domain == base_domain))
        return self._to_site_dto(db_site) if db_site else None

    async def get_all_sites(self) -> list[SiteDto]:
        result = await self.session.scalars(select(Site).order_by(Site.base_domain.asc()))
        return [self._to_site_dto(site) for site in cast(list, result.all())]

    async def update_site(self, site: SiteDto) -> Optional[SiteDto]:
        if not site.changed_data:
            return None

        values = {key: getattr(site, key) for key in site.changed_data}
        stmt = update(Site).where(Site.id == site.id).values(**values).returning(Site)
        db_site = await self.session.scalar(stmt)
        return self._to_site_dto(db_site) if db_site else None

    async def create_subdomain(self, subdomain: SiteSubdomainDto) -> SiteSubdomainDto:
        db_subdomain = SiteSubdomain(
            site_id=subdomain.site_id,
            host=subdomain.host,
            is_active=subdomain.is_active,
        )
        self.session.add(db_subdomain)
        await self.session.flush()
        logger.debug(f"Created subdomain '{subdomain.host}' for site '{subdomain.site_id}'")
        return self._to_subdomain_dto(db_subdomain)

    async def get_subdomain_by_id(self, subdomain_id: int) -> Optional[SiteSubdomainDto]:
        db_subdomain = await self.session.scalar(
            select(SiteSubdomain).where(SiteSubdomain.id == subdomain_id)
        )
        return self._to_subdomain_dto(db_subdomain) if db_subdomain else None

    async def get_subdomain_by_host(self, host: str) -> Optional[SiteSubdomainDto]:
        db_subdomain = await self.session.scalar(
            select(SiteSubdomain).where(SiteSubdomain.host == host)
        )
        return self._to_subdomain_dto(db_subdomain) if db_subdomain else None

    async def get_subdomains_by_site_id(self, site_id: int) -> list[SiteSubdomainDto]:
        result = await self.session.scalars(
            select(SiteSubdomain)
            .where(SiteSubdomain.site_id == site_id)
            .order_by(SiteSubdomain.host.asc())
        )
        return [self._to_subdomain_dto(item) for item in cast(list, result.all())]

    async def update_subdomain(self, subdomain: SiteSubdomainDto) -> Optional[SiteSubdomainDto]:
        if not subdomain.changed_data:
            return None

        values = {key: getattr(subdomain, key) for key in subdomain.changed_data}
        stmt = (
            update(SiteSubdomain)
            .where(SiteSubdomain.id == subdomain.id)
            .values(**values)
            .returning(SiteSubdomain)
        )
        db_subdomain = await self.session.scalar(stmt)
        return self._to_subdomain_dto(db_subdomain) if db_subdomain else None

    async def create_keyword(self, keyword: SiteKeywordDto) -> SiteKeywordDto:
        db_keyword = SiteKeyword(
            subdomain_id=keyword.subdomain_id,
            keyword=keyword.keyword,
            is_active=keyword.is_active,
        )
        self.session.add(db_keyword)
        await self.session.flush()
        logger.debug(
            f"Created keyword '{keyword.keyword}' for subdomain '{keyword.subdomain_id}'"
        )
        return self._to_keyword_dto(db_keyword)

    async def get_keyword_by_id(self, keyword_id: int) -> Optional[SiteKeywordDto]:
        db_keyword = await self.session.scalar(
            select(SiteKeyword).where(SiteKeyword.id == keyword_id)
        )
        return self._to_keyword_dto(db_keyword) if db_keyword else None

    async def get_keywords_by_subdomain_id(self, subdomain_id: int) -> list[SiteKeywordDto]:
        result = await self.session.scalars(
            select(SiteKeyword)
            .where(SiteKeyword.subdomain_id == subdomain_id)
            .order_by(SiteKeyword.keyword.asc())
        )
        return [self._to_keyword_dto(item) for item in cast(list, result.all())]

    async def update_keyword(self, keyword: SiteKeywordDto) -> Optional[SiteKeywordDto]:
        if not keyword.changed_data:
            return None

        values = {key: getattr(keyword, key) for key in keyword.changed_data}
        stmt = (
            update(SiteKeyword)
            .where(SiteKeyword.id == keyword.id)
            .values(**values)
            .returning(SiteKeyword)
        )
        db_keyword = await self.session.scalar(stmt)
        return self._to_keyword_dto(db_keyword) if db_keyword else None

    async def delete_keyword(self, keyword_id: int) -> bool:
        stmt = delete(SiteKeyword).where(SiteKeyword.id == keyword_id)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)
