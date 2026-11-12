"""Удаление портфеля освобождает исходники, сохраняя отдельные отчёты."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Response
from sqlalchemy import select

from svetofor.api.deps import CurrentUser, DbSession
from svetofor.api.errors import AppError, forbidden
from svetofor.core.config import get_settings
from svetofor.db.enums import Role
from svetofor.db.models import Check, User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.snapshot import ACTIVE, portfolio_for

router = APIRouter(prefix="/api/portfolios", tags=["Портфель"])


@router.delete("/{portfolio_id}", status_code=204)
def delete_portfolio(portfolio_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    portfolio = portfolio_for(db, portfolio_id, user)
    if portfolio.owner_id != user.id and user.role != Role.ADMIN:
        raise forbidden("Удалить портфель может владелец или администратор")
    db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
    db.execute(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
    items = list(
        db.scalars(
            select(PortfolioItem)
            .where(
                PortfolioItem.portfolio_id == portfolio_id,
            )
            .with_for_update()
        )
    )
    for item in items:
        check = db.get(Check, item.check_id) if item.check_id else None
        if item.edit_token or item.status == "starting" or (check and check.status in ACTIVE):
            raise AppError("busy", "Дождитесь завершения активных проверок", 409)
    sources = [Path(item.source_path).resolve() for item in items if item.source_path]
    db.delete(portfolio)
    db.commit()
    root = (get_settings().data_dir / "portfolio_uploads").resolve()
    for source in sources:
        if source.is_relative_to(root):
            source.unlink(missing_ok=True)
    return Response(status_code=204)
