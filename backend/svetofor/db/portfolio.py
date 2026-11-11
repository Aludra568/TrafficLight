"""Портфель хранит источники и ссылки на конкретные запуски проверок."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from svetofor.db.models import Base, IdMixin, fk


class Portfolio(IdMixin, Base):
    __tablename__ = "portfolios"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_portfolio_request"),)

    owner_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_token: Mapped[str | None] = mapped_column(String(36))
    dispatch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ruleset_version_id: Mapped[uuid.UUID | None] = fk(
        "ruleset_versions.id",
        ondelete="SET NULL",
        nullable=True,
    )
    items: Mapped[list[PortfolioItem]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PortfolioItem.position",
    )


class PortfolioItem(IdMixin, Base):
    __tablename__ = "portfolio_items"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "position", name="uq_portfolio_position"),
        CheckConstraint("source_type IN ('file', 'eis')", name="source_type"),
        CheckConstraint("status IN ('waiting', 'starting', 'checking', 'failed')", name="status"),
        CheckConstraint(
            "position >= 0 AND attempts >= 0 AND source_bytes >= 0", name="nonnegative"
        ),
    )

    portfolio_id: Mapped[uuid.UUID] = fk("portfolios.id")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_name: Mapped[str] = mapped_column(String(500), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    check_id: Mapped[uuid.UUID | None] = fk("checks.id", ondelete="SET NULL", nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="waiting", server_default="waiting"
    )
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    portfolio: Mapped[Portfolio] = relationship(back_populates="items")
