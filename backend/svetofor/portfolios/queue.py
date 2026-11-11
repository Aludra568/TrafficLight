"""Резервирование мест: портфель не обходит пользовательский лимит проверок."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from svetofor.core.config import get_settings
from svetofor.db.models import Check, Package, User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.snapshot import ACTIVE, DONE

LEASE = timedelta(minutes=10)


def claim_items(db: Session, portfolio_id: uuid.UUID) -> list[tuple[uuid.UUID, int]]:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return []
    if portfolio.owner_id is None:
        for item in portfolio.items:
            if item.status in {"waiting", "starting"}:
                item.status, item.error = "failed", "Владелец портфеля удалён"
        return []
    user = db.scalar(select(User).where(User.id == portfolio.owner_id).with_for_update())
    if user is None or not user.is_active:
        for item in portfolio.items:
            if item.status in {"waiting", "starting"}:
                item.status, item.error = "failed", "Владелец портфеля отключён"
        return []
    now = datetime.now(UTC)
    entries = list(
        db.scalars(
            select(PortfolioItem)
            .where(
                PortfolioItem.portfolio_id == portfolio_id,
            )
            .order_by(PortfolioItem.position)
            .with_for_update()
        )
    )
    for item in entries:
        check = db.get(Check, item.check_id) if item.check_id else None
        if check and check.status in DONE and item.source_path:
            source = Path(item.source_path).resolve()
            if source.is_relative_to((get_settings().data_dir / "portfolio_uploads").resolve()):
                source.unlink(missing_ok=True)
                item.source_path = None
        if item.status == "starting" and item.started_at and item.started_at < now - LEASE:
            item.status, item.error = "failed", "Загрузка прервалась. Повторите неуспешную закупку"
    active = (
        db.scalar(
            select(func.count(Check.id))
            .join(Package)
            .where(
                Package.owner_id == user.id,
                Check.status.in_(ACTIVE),
            )
        )
        or 0
    )
    reserved = (
        db.scalar(
            select(func.count(PortfolioItem.id))
            .join(Portfolio)
            .where(
                Portfolio.owner_id == user.id,
                PortfolioItem.status == "starting",
            )
        )
        or 0
    )
    own_busy = sum(
        item.status == "starting"
        or (
            item.check_id is not None
            and (check := db.get(Check, item.check_id)) is not None
            and check.status in ACTIVE
        )
        for item in entries
    )
    settings = get_settings()
    free = min(
        settings.max_active_checks_per_user - active - reserved,
        settings.portfolio_parallel_checks - own_busy,
    )
    claimed = []
    for item in entries:
        if free <= 0:
            break
        if item.status != "waiting":
            continue
        item.status, item.started_at, item.error = "starting", now, None
        item.attempts += 1
        claimed.append((item.id, item.attempts))
        free -= 1
    return claimed


def pending(db: Session, portfolio_id: uuid.UUID) -> bool:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return False
    for item in portfolio.items:
        if item.status in {"waiting", "starting"}:
            return True
        check = db.get(Check, item.check_id) if item.check_id else None
        if check and check.status in ACTIVE:
            return True
    return False
