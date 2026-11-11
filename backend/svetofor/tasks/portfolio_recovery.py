"""Восстановление цепочки после гибели диспетчера между сменой токена и постановкой задачи."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from svetofor.core.logging import get_logger
from svetofor.db.enums import CheckStatus
from svetofor.db.models import Check, User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.portfolios.dispatch import enqueue
from svetofor.portfolios.queue import pending
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import run_check, services

log = get_logger(__name__)


def requeue_unstarted(factory: Any, identity: uuid.UUID) -> None:
    """Коммит проверки мог пройти перед гибелью процесса, а отправка в брокер — нет."""
    now = datetime.now(UTC)
    with factory() as db:
        portfolio = db.get(Portfolio, identity)
        if portfolio is None or portfolio.owner_id is None:
            return
        db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
        rows = list(
            db.scalars(
                select(PortfolioItem)
                .join(Check)
                .where(
                    PortfolioItem.portfolio_id == identity,
                    PortfolioItem.status == "checking",
                    Check.status == CheckStatus.QUEUED,
                    PortfolioItem.started_at < now - timedelta(seconds=30),
                )
                .with_for_update(of=PortfolioItem)
            )
        )
        identities = [item.check_id for item in rows]
        for item in rows:
            item.started_at = now
        db.commit()
    for check_id in identities:
        try:
            # start_check берёт блокировку проверки; повтор не запускает второй конвейер.
            run_check.delay(str(check_id), True)
        except Exception:
            log.exception("portfolio_start_retry_failed", check_id=str(check_id))


def arm_recovery(factory: Any, identity: uuid.UUID) -> None:
    with factory() as db:
        portfolio = db.get(Portfolio, identity)
        if not portfolio or not portfolio.dispatch_token or not portfolio.dispatch_until:
            return
        token = portfolio.dispatch_token
        seconds = max(1, (portfolio.dispatch_until - datetime.now(UTC)).total_seconds() + 1)
    recover_dispatch.apply_async(args=[str(identity), token], countdown=seconds)


@celery_app.task(name="portfolios.recover", soft_time_limit=30, time_limit=45)
def recover_dispatch(portfolio_id: str, token: str) -> None:
    s = services()
    identity = uuid.UUID(portfolio_id)
    with s.session_factory() as db:
        portfolio = db.scalar(select(Portfolio).where(Portfolio.id == identity).with_for_update())
        if (
            portfolio is None
            or portfolio.dispatch_token != token
            or portfolio.dispatch_until is None
            or portfolio.dispatch_until > datetime.now(UTC)
        ):
            return
        more = pending(db, identity)
        portfolio.dispatch_token, portfolio.dispatch_until = None, None
        db.commit()
    if more:
        enqueue(identity, s.session_factory)


@celery_app.task(name="portfolios.sweep", soft_time_limit=30, time_limit=45)
def sweep_portfolios() -> None:
    """Независимый сторож восстанавливает и окно между коммитом recovery и отправкой."""
    s = services()
    with s.session_factory() as db:
        identities = list(
            db.scalars(
                select(Portfolio.id)
                .join(PortfolioItem)
                .outerjoin(Check)
                .where(
                    or_(
                        Portfolio.dispatch_until.is_(None),
                        Portfolio.dispatch_until <= datetime.now(UTC),
                    ),
                    or_(
                        PortfolioItem.status.in_(["waiting", "starting"]),
                        Check.status.in_(["queued", "running"]),
                    ),
                )
                .distinct()
                .limit(100)
            )
        )
    for identity in identities:
        enqueue(identity, s.session_factory)
