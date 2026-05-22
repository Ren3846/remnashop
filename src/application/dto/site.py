from dataclasses import dataclass

from .base import BaseDto, TimestampMixin, TrackableMixin


@dataclass(kw_only=True)
class SiteDto(BaseDto, TrackableMixin, TimestampMixin):
    name: str
    base_domain: str
    is_active: bool = True


@dataclass(kw_only=True)
class SiteSubdomainDto(BaseDto, TrackableMixin, TimestampMixin):
    site_id: int
    host: str
    is_active: bool = True


@dataclass(kw_only=True)
class SiteKeywordDto(BaseDto, TrackableMixin, TimestampMixin):
    subdomain_id: int
    keyword: str
    is_active: bool = True
