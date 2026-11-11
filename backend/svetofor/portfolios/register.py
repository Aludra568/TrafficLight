"""Регистрация запуска: ключ повторного запроса и единая транзакция метаданных."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.admission import ensure_capacity
from svetofor.portfolios.uploads import Source, request_hash
from svetofor.rules.store import latest_ruleset_version


def register(
    db: Session,
    user_id: uuid.UUID,
    title: str,
    key: str,
    sources: list[Source],
) -> tuple[Portfolio, bool]:
    # Строка владельца сериализует одинаковые ключи, включая два одновременных запроса.
    db.execute(select(User.id).where(User.id == user_id).with_for_update())
    previous = db.scalar(
        select(Portfolio).where(
            Portfolio.owner_id == user_id,
            Portfolio.request_key == key,
        )
    )
    digest = request_hash(title, sources)
    if previous is not None:
        if previous.request_hash != digest:
            raise AppError(
                "request_conflict", "Этот ключ уже использован для другого портфеля", 409
            )
        return previous, False
    ensure_capacity(db, user_id, sum(source.source_bytes for source in sources))
    version = latest_ruleset_version(db, get_settings().default_ruleset)
    if version is None:
        raise AppError("ruleset_missing", "Набор правил не загружен", 409)
    portfolio = Portfolio(
        owner_id=user_id,
        title=title,
        request_key=key,
        request_hash=digest,
        ruleset_version_id=version.id,
    )
    db.add(portfolio)
    db.flush()
    for index, source in enumerate(sources):
        db.add(PortfolioItem(portfolio_id=portfolio.id, position=index, **asdict(source)))
    db.flush()
    return portfolio, True
