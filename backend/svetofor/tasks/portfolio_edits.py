"""Подготовка редакции в очереди; старая проверка остаётся до фиксации новой загрузки."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.core.config import get_settings
from svetofor.core.logging import get_logger
from svetofor.db.models import User
from svetofor.db.portfolio import Portfolio, PortfolioItem, PortfolioOperation
from svetofor.pipeline.ingest import file_sha256
from svetofor.portfolios.dispatch import enqueue
from svetofor.portfolios.edit_capacity import capacity
from svetofor.portfolios.edit_files import assemble, cleanup_attempt, cleanup_uploads
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import services

log = get_logger(__name__)


def fail_operation(
    factory: Callable[[], Session], identity: uuid.UUID, message: str, started_at: datetime
) -> PortfolioOperation | None:
    with factory() as db:
        op = db.get(PortfolioOperation, identity)
        if op is None or op.status != "running" or op.started_at != started_at:
            return None
        item = db.get(PortfolioItem, uuid.UUID(op.payload["item_id"]), with_for_update=True)
        op = db.get(PortfolioOperation, identity, with_for_update=True, populate_existing=True)
        if op is None or op.status != "running" or op.started_at != started_at:
            return None
        if item and item.edit_token == str(op.id):
            item.edit_token, item.edit_error = None, message
            item.source_bytes = op.payload["previous_bytes"]
            if op.kind == "bundle":
                item.status, item.error = "failed", message
        op.status, op.error = "failed", message
        db.commit()
        db.expunge(op)
        return op


@celery_app.task(name="portfolios.prepare_edit", soft_time_limit=480, time_limit=510)
def prepare_edit(operation_id: str) -> None:
    s = services()
    identity = uuid.UUID(operation_id)
    with s.session_factory() as db:
        op = db.get(PortfolioOperation, identity, with_for_update=True)
        if op is None or op.status != "queued":
            return
        op.status, op.started_at = "running", datetime.now(UTC)
        db.commit()
        db.expunge(op)
    target = None
    committed = False
    try:
        target = assemble(s.session_factory, op, s.settings)
        size, digest = target.stat().st_size, file_sha256(target)
        with s.session_factory() as db:
            portfolio = db.get(Portfolio, op.portfolio_id)
            if portfolio is None:
                target.unlink(missing_ok=True)
                return
            db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
            portfolio = db.get(Portfolio, op.portfolio_id, with_for_update=True)
            item = db.get(PortfolioItem, uuid.UUID(op.payload["item_id"]), with_for_update=True)
            current = db.get(PortfolioOperation, identity, with_for_update=True)
            if (
                not portfolio
                or not item
                or not current
                or current.status != "running"
                or current.started_at != op.started_at
                or item.edit_token != str(identity)
            ):
                target.unlink(missing_ok=True)
                return
            entries = list(
                db.scalars(select(PortfolioItem).where(PortfolioItem.portfolio_id == portfolio.id))
            )
            capacity(
                db,
                portfolio,
                size - item.source_bytes,
                sum(i.source_bytes for i in entries) - item.source_bytes + size,
            )
            item.source_type, item.source_name = "file", "Документы закупки.zip"
            item.source_path, item.source_sha256, item.source_bytes = str(target), digest, size
            item.replace_source = item.check_id is not None
            item.edit_token, item.edit_error, item.error = None, None, None
            item.status, item.started_at = "waiting", None
            current.status, current.error = "done", None
            db.commit()
            committed = True
        cleanup_uploads(op, s.settings)
        dispatch_error = enqueue(op.portfolio_id, s.session_factory)
        if dispatch_error:
            with s.session_factory() as db:
                item = db.get(PortfolioItem, uuid.UUID(op.payload["item_id"]), with_for_update=True)
                if item and item.source_path == str(target) and not item.edit_token:
                    item.edit_error = dispatch_error
                    db.commit()
    except Exception:
        # Не передаём текст исключения наружу: он может содержать путь хранилища.
        if committed:
            log.warning("portfolio_edit_followup_failed", operation_id=str(identity))
            return
        if target:
            target.unlink(missing_ok=True)
        failed = fail_operation(
            s.session_factory,
            identity,
            "Не удалось подготовить документы. Предыдущий отчёт сохранён",
            op.started_at,
        )
        if failed:
            cleanup_attempt(failed, s.settings)


def recover_edits(factory: Callable[[], Session]) -> None:
    with factory() as db:
        queued = list(
            db.scalars(
                select(PortfolioOperation.id)
                .where(PortfolioOperation.status == "queued")
                .limit(100)
            )
        )
        expired = list(
            db.scalars(
                select(PortfolioOperation).where(
                    PortfolioOperation.status == "running",
                    PortfolioOperation.started_at < datetime.now(UTC) - timedelta(minutes=10),
                )
            )
        )
    for operation in expired:
        if operation.started_at is None:
            continue
        failed = fail_operation(
            factory,
            operation.id,
            "Подготовка прервалась. Предыдущий отчёт сохранён",
            operation.started_at,
        )
        if failed:
            cleanup_attempt(failed, get_settings())
    for identity in queued:
        prepare_edit.delay(str(identity))
