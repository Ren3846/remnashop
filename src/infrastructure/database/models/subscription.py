from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from remnapy.enums import TrafficLimitStrategy
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.enums import SubscriptionStatus

from .base import BaseSql
from .timestamp import TimestampMixin
from .user import User


class Subscription(BaseSql, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Numeric Remnawave user id (panel >= 3.0, BIGINT). NULL only for a subscription the
    # uuid -> id migration left explicitly unlinked; never sent to the panel.
    user_remna_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    # Panel uuid from Remnawave 2.x, kept for audit and restore after the uuid -> id migration.
    user_remna_uuid_legacy: Mapped[Optional[UUID]] = mapped_column(nullable=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    status: Mapped[SubscriptionStatus] = mapped_column(index=True)
    is_trial: Mapped[bool]
    disabled_by_channel_leave: Mapped[bool] = mapped_column(default=False, server_default="false")

    traffic_limit: Mapped[int]
    device_limit: Mapped[int]
    traffic_limit_strategy: Mapped[TrafficLimitStrategy]

    tag: Mapped[Optional[str]]

    internal_squads: Mapped[list[UUID]]
    external_squad: Mapped[Optional[UUID]]

    expire_at: Mapped[datetime] = mapped_column(index=True)
    url: Mapped[str]

    plan_snapshot: Mapped[dict[str, Any]]
    device_single_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    device_all_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    link_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
