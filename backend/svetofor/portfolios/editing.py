"""Транзакционные изменения состава, без тяжёлой обработки файлов."""

import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import Check, Document
from svetofor.db.portfolio import Portfolio, PortfolioItem, PortfolioOperation
from svetofor.portfolios.edit_access import check_version, ensure_idle, find_item, lock_items
from svetofor.portfolios.edit_capacity import capacity
from svetofor.portfolios.edit_operations import (
    digest,
    operation,
    previous_operation,
    retry_preparation,
)
from svetofor.portfolios.uploads import Source, validate_count


def add_sources(
    db: Session, portfolio: Portfolio, key: str, sources: list[Source], bundle: bool
) -> tuple[PortfolioOperation, bool]:
    fingerprint = digest("add", sources, bundle)
    previous = previous_operation(db, portfolio, key, fingerprint)
    if previous:
        return previous, retry_preparation(db, previous, sources)
    entries = lock_items(db, portfolio.id)
    if any(
        s.source_type == "eis"
        and any(i.source_type == "eis" and i.source_name == s.source_name for i in entries)
        for s in sources
    ):
        raise AppError("duplicate_number", "Закупка с этим номером уже есть в портфеле", 409)
    validate_count(len(entries) + (1 if bundle else len(sources)))
    size = sum(s.source_bytes for s in sources)
    capacity(db, portfolio, size, sum(i.source_bytes for i in entries) + size)
    start = max((i.position for i in entries), default=-1) + 1
    if bundle:
        item = PortfolioItem(
            portfolio_id=portfolio.id,
            position=start,
            source_type="file",
            source_name="Документы закупки.zip",
            source_bytes=size,
        )
        db.add(item)
        db.flush()
        op = operation(
            db,
            portfolio,
            key,
            fingerprint,
            "bundle",
            {
                "item_id": str(item.id),
                "sources": [asdict(s) for s in sources],
                "check_id": None,
                "remove_ids": [],
                "version": 0,
                "previous_bytes": 0,
                "reserved_bytes": size,
            },
        )
        item.edit_token = str(op.id)
    else:
        for index, source in enumerate(sources):
            db.add(
                PortfolioItem(portfolio_id=portfolio.id, position=start + index, **asdict(source))
            )
        op = operation(db, portfolio, key, fingerprint, "add", {})
        op.status = "done"
    return op, True


def begin_documents(
    db: Session,
    portfolio: Portfolio,
    item_id: uuid.UUID,
    key: str,
    version: int,
    remove: list[uuid.UUID],
    sources: list[Source],
) -> tuple[PortfolioOperation, bool]:
    fingerprint = digest("documents", sources, [str(item_id), version, sorted(map(str, remove))])
    previous = previous_operation(db, portfolio, key, fingerprint)
    if previous:
        return previous, retry_preparation(db, previous, sources)
    entries = lock_items(db, portfolio.id)
    item = find_item(entries, item_id)
    check_version(item, version)
    ensure_idle(db, item, documents=True)
    check = db.get(Check, item.check_id)
    if check is None:
        raise AppError("not_found", "Проверка удалена", 404)
    docs = list(db.scalars(select(Document).where(Document.package_id == check.package_id)))
    if not set(remove) <= {d.id for d in docs}:
        raise AppError("document_missing", "Документ не принадлежит этой закупке", 404)
    kept = [d for d in docs if d.id not in remove]
    if not kept and not sources:
        raise AppError("empty_documents", "Оставьте хотя бы один документ", 400)
    if not remove and not sources:
        raise AppError("no_changes", "Состав документов не изменён", 400)
    if len(kept) + len(sources) > get_settings().max_files:
        raise AppError("too_many_files", "Превышено число документов", 400)
    size = sum(d.size_bytes for d in kept) + sum(s.source_bytes for s in sources)
    if size > get_settings().max_upload_bytes:
        raise AppError("too_large", "Превышен размер пакета", 413)
    previous_stored = item.source_bytes if item.source_path or item.edit_token else 0
    capacity(
        db,
        portfolio,
        size - previous_stored,
        sum(i.source_bytes for i in entries) - item.source_bytes + size,
    )
    op = operation(
        db,
        portfolio,
        key,
        fingerprint,
        "documents",
        {
            "item_id": str(item.id),
            "sources": [asdict(s) for s in sources],
            "check_id": str(check.id),
            "remove_ids": list(map(str, remove)),
            "version": version + 1,
            "previous_bytes": item.source_bytes,
            "reserved_bytes": size,
        },
    )
    item.edit_token, item.edit_error = str(op.id), None
    item.source_bytes = size
    item.version += 1
    return op, True
