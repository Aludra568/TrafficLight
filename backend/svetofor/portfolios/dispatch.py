"""Аренда диспетчера: повтор HTTP и повторная доставка Celery не размножают цепочки."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from svetofor.core.logging import get_logger
from svetofor.db.portfolio import Portfolio

log = get_logger(__name__)
LEASE = timedelta(seconds=90)


def enqueue(portfolio_id: uuid.UUID, factory: Any, *, recover: bool = False) -> str | None:
    from svetofor.tasks.portfolios import dispatch_portfolio

    with factory() as db:
        portfolio = db.scalar(
            select(Portfolio)
            .where(
                Portfolio.id == portfolio_id,
            )
            .with_for_update()
        )
        if portfolio is None:
            return None
        now = datetime.now(UTC)
        if not recover and portfolio.dispatch_until and portfolio.dispatch_until > now:
            return None
        token = str(uuid.uuid4())
        portfolio.dispatch_token, portfolio.dispatch_until = token, now + LEASE
        db.commit()
    try:
        dispatch_portfolio.delay(str(portfolio_id), token)
    except Exception:
        with factory() as db:
            db.execute(
                update(Portfolio)
                .where(
                    Portfolio.id == portfolio_id,
                    Portfolio.dispatch_token == token,
                )
                .values(dispatch_token=None, dispatch_until=None)
            )
            db.commit()
        log.exception("portfolio_enqueue_failed", portfolio_id=str(portfolio_id))
        return "Очередь недоступна. Портфель сохранён; повторите запуск на его странице"
    return None


def next_token(factory: Any, identity: uuid.UUID, token: str) -> str | None:
    successor = str(uuid.uuid4())
    with factory() as db:
        changed = db.execute(
            update(Portfolio)
            .where(
                Portfolio.id == identity,
                Portfolio.dispatch_token == token,
            )
            .values(dispatch_token=successor, dispatch_until=datetime.now(UTC) + LEASE)
            .returning(Portfolio.id)
        ).scalar_one_or_none()
        db.commit()
    return successor if changed is not None else None


def release(factory: Any, identity: uuid.UUID, token: str) -> None:
    with factory() as db:
        db.execute(
            update(Portfolio)
            .where(
                Portfolio.id == identity,
                Portfolio.dispatch_token == token,
            )
            .values(dispatch_token=None, dispatch_until=None)
        )
        db.commit()
