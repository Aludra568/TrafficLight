"""Устойчивая диспетчеризация портфеля поверх существующих очередей Celery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from celery.signals import worker_ready
from sqlalchemy import or_, select

from svetofor.core.logging import get_logger
from svetofor.db.models import Check, User
from svetofor.db.portfolio import Portfolio, PortfolioItem
from svetofor.pipeline.eis import EisError
from svetofor.pipeline.finalize import fail_check, fail_stalled
from svetofor.pipeline.ingest import create_check, new_check
from svetofor.portfolios.dispatch import enqueue, next_token, release
from svetofor.portfolios.processing import prepare_source
from svetofor.portfolios.queue import claim_items, pending
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import run_check, services

log = get_logger(__name__)


def _item_error(factory: Any, item_id: uuid.UUID, attempt: int, message: str) -> None:
    with factory() as db:
        item = db.scalar(
            select(PortfolioItem)
            .where(
                PortfolioItem.id == item_id,
            )
            .with_for_update()
        )
        if item and item.attempts == attempt and item.status == "starting":
            item.status, item.error = "failed", message
            db.commit()


@celery_app.task(name="portfolios.process", soft_time_limit=480, time_limit=510)
def process_portfolio_item(item_id: str, attempt: int) -> None:
    s = services()
    identity = uuid.UUID(item_id)
    incoming = None
    try:
        with s.session_factory() as db:
            item = db.get(PortfolioItem, identity)
            if not item or item.status != "starting" or item.attempts != attempt:
                return
            previous = (
                db.get(Check, item.check_id) if item.check_id and not item.replace_source else None
            )
            if previous is None:
                incoming, source_bytes = prepare_source(item, s)
            portfolio_id = item.portfolio_id
        with s.session_factory() as db:
            portfolio = db.get(Portfolio, portfolio_id)
            if portfolio is None:
                return
            db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
            portfolio = db.scalar(
                select(Portfolio)
                .where(
                    Portfolio.id == portfolio_id,
                )
                .with_for_update()
            )
            item = db.scalar(
                select(PortfolioItem)
                .where(
                    PortfolioItem.id == identity,
                )
                .with_for_update()
            )
            if not item or item.status != "starting" or item.attempts != attempt:
                return
            if portfolio is None or portfolio.owner_id is None:
                item.status, item.error = "failed", "Владелец портфеля не найден"
                db.commit()
                return
            previous = (
                db.get(Check, item.check_id) if item.check_id and not item.replace_source else None
            )
            if item.source_type == "eis" and incoming is not None:
                used = sum(row.source_bytes for row in portfolio.items if row.id != item.id)
                size = source_bytes
                if used + size > s.settings.max_portfolio_mb * 1024**2:
                    item.status, item.error = "failed", "Превышен общий размер портфеля"
                    db.commit()
                    return
                item.source_bytes = size
            if previous is not None:
                check = new_check(db, previous.package, portfolio.ruleset_version_id)
            elif incoming is not None:
                check = create_check(
                    db,
                    upload=incoming,
                    original_name=(
                        f"Закупка № {item.source_name}.zip"
                        if item.source_type == "eis"
                        else item.source_name
                    ),
                    config=s.ingest,
                    ruleset_version_id=portfolio.ruleset_version_id,
                    owner_id=portfolio.owner_id,
                )
            else:
                item.status, item.error = "failed", "Исходный пакет удалён"
                db.commit()
                return
            item.check_id, item.status, item.error = check.id, "checking", None
            item.replace_source = False
            item.started_at = datetime.now(UTC)
            check_id = check.id
            db.commit()
        try:
            run_check.delay(str(check_id), True)
        except Exception:
            fail_check(
                s.session_factory, check_id, "Очередь недоступна. Повторите проверку закупки"
            )
            log.exception("portfolio_check_enqueue_failed", check_id=str(check_id))
    except EisError as exc:
        _item_error(s.session_factory, identity, attempt, str(exc))
    except Exception:  # сбой одного источника не отменяет остальные закупки
        log.exception("portfolio_source_failed", item_id=item_id)
        _item_error(
            s.session_factory, identity, attempt, "Не удалось подготовить закупку. Повторите запуск"
        )
    finally:
        if incoming is not None:
            incoming.unlink(missing_ok=True)


@celery_app.task(name="portfolios.dispatch", soft_time_limit=60, time_limit=90)
def dispatch_portfolio(portfolio_id: str, token: str) -> None:
    s = services()
    identity = uuid.UUID(portfolio_id)
    successor = next_token(s.session_factory, identity, token)
    if successor is None:
        if not celery_app.conf.task_always_eager:
            from svetofor.tasks.portfolio_recovery import arm_recovery

            arm_recovery(s.session_factory, identity)
        return
    from svetofor.tasks.portfolio_recovery import requeue_unstarted

    requeue_unstarted(s.session_factory, identity)
    while True:
        with s.session_factory() as db:
            check_ids = list(
                db.scalars(
                    select(Check.id)
                    .join(PortfolioItem)
                    .where(
                        PortfolioItem.portfolio_id == identity,
                    )
                )
            )
        if check_ids:
            fail_stalled(s.session_factory, check_ids)
        with s.session_factory() as db:
            claimed = claim_items(db, identity)
            db.commit()
        for item_id, attempt in claimed:
            try:
                process_portfolio_item.delay(str(item_id), attempt)
            except Exception:
                _item_error(
                    s.session_factory, item_id, attempt, "Очередь недоступна. Повторите запуск"
                )
                log.exception("portfolio_item_enqueue_failed", item_id=str(item_id))
        if not celery_app.conf.task_always_eager or not claimed:
            break
    with s.session_factory() as db:
        more = pending(db, identity)
    if more and not celery_app.conf.task_always_eager:
        dispatch_portfolio.apply_async(args=[portfolio_id, successor], countdown=5)
    else:
        release(s.session_factory, identity, successor)


@worker_ready.connect
def resume_portfolios(**_: Any) -> None:
    """После рестарта брокера/воркера восстанавливаем очередь из БД, не из памяти процесса."""
    s = services()
    with s.session_factory() as db:
        identities = list(
            db.scalars(
                select(Portfolio.id)
                .join(PortfolioItem)
                .outerjoin(Check)
                .where(
                    or_(
                        PortfolioItem.status.in_(["waiting", "starting"]),
                        Check.status.in_(["queued", "running"]),
                    ),
                )
                .distinct()
            )
        )
    for identity in identities:
        enqueue(identity, s.session_factory, recover=True)
