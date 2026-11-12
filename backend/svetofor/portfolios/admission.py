"""Квота ожидающих запусков и сохранённых исходников одного владельца."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session

from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import Check
from svetofor.db.portfolio import Portfolio, PortfolioItem


def ensure_capacity(
    db: Session, owner: uuid.UUID, new_bytes: int, exclude_portfolio: uuid.UUID | None = None
) -> None:
    pending = (
        select(Portfolio.id)
        .join(PortfolioItem)
        .outerjoin(Check)
        .where(
            Portfolio.owner_id == owner,
            Portfolio.id != exclude_portfolio if exclude_portfolio else true(),
            or_(
                PortfolioItem.edit_token.is_not(None),
                PortfolioItem.status.in_(["waiting", "starting"]),
                Check.status.in_(["queued", "running"]),
            ),
        )
        .distinct()
        .subquery()
    )
    count = db.scalar(select(func.count()).select_from(pending)) or 0
    stored = (
        db.scalar(
            select(func.sum(PortfolioItem.source_bytes))
            .join(Portfolio)
            .where(
                Portfolio.owner_id == owner,
                or_(PortfolioItem.source_path.is_not(None), PortfolioItem.edit_token.is_not(None)),
            )
        )
        or 0
    )
    settings = get_settings()
    if count >= settings.max_pending_portfolios:
        raise AppError(
            "portfolio_quota", "Дождитесь завершения одного из запущенных портфелей", 429
        )
    if stored + new_bytes > settings.max_portfolio_storage_mb * 1024**2:
        raise AppError(
            "portfolio_storage", "Удалите завершённые портфели с сохранёнными исходниками", 429
        )
