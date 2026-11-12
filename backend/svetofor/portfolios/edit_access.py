"""Права и единый порядок блокировок при редактировании портфеля."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.errors import AppError, forbidden, not_found
from svetofor.db.enums import Role
from svetofor.db.models import Check, User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.snapshot import ACTIVE, portfolio_for


def editable_portfolio(db: Session, identity: uuid.UUID, user: User) -> Portfolio:
    portfolio = portfolio_for(db, identity, user)
    if portfolio.owner_id != user.id and user.role != Role.ADMIN:
        raise forbidden("Изменить портфель может владелец или администратор")
    if portfolio.owner_id is None:
        raise AppError("owner_missing", "Владелец портфеля удалён", 409)
    return portfolio


def lock_portfolio(db: Session, identity: uuid.UUID, user: User) -> Portfolio:
    portfolio = editable_portfolio(db, identity, user)
    db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
    return db.scalars(
        select(Portfolio)
        .where(Portfolio.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def lock_items(db: Session, portfolio_id: uuid.UUID) -> Sequence[PortfolioItem]:
    return db.scalars(
        select(PortfolioItem)
        .where(PortfolioItem.portfolio_id == portfolio_id)
        .order_by(PortfolioItem.position)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def find_item(items: Sequence[PortfolioItem], item_id: uuid.UUID) -> PortfolioItem:
    item = next((i for i in items if i.id == item_id), None)
    if item is None:
        raise not_found("Закупка портфеля")
    return item


def check_version(item: PortfolioItem, version: int) -> None:
    if item.version != version:
        raise AppError("edit_conflict", "Состав закупки изменён. Обновите портфель", 409)


def ensure_idle(db: Session, item: PortfolioItem, *, documents: bool = False) -> None:
    check = db.get(Check, item.check_id) if item.check_id else None
    if item.edit_token or item.status == "starting" or (check and check.status in ACTIVE):
        raise AppError("busy", "Дождитесь завершения проверки этой закупки", 409)
    if documents and (check is None or item.status == "waiting" or item.replace_source):
        raise AppError("not_ready", "Документы доступны после обработки пакета", 409)
