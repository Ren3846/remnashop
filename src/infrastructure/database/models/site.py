from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import BaseSql
from .timestamp import TimestampMixin


class Site(BaseSql, TimestampMixin):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    base_domain: Mapped[str] = mapped_column(String(253), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    subdomains: Mapped[list["SiteSubdomain"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )


class SiteSubdomain(BaseSql, TimestampMixin):
    __tablename__ = "site_subdomains"
    __table_args__ = (UniqueConstraint("host", name="uq_site_subdomains_host"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    site: Mapped["Site"] = relationship(back_populates="subdomains")
    keywords: Mapped[list["SiteKeyword"]] = relationship(
        back_populates="subdomain",
        cascade="all, delete-orphan",
    )


class SiteKeyword(BaseSql, TimestampMixin):
    __tablename__ = "site_keywords"
    __table_args__ = (UniqueConstraint("subdomain_id", "keyword", name="uq_site_keywords_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subdomain_id: Mapped[int] = mapped_column(
        ForeignKey("site_subdomains.id", ondelete="CASCADE"),
        index=True,
    )
    keyword: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(default=True)

    subdomain: Mapped["SiteSubdomain"] = relationship(back_populates="keywords")
