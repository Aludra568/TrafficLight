"""Идемпотентные квитанции операций редактирования."""

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.errors import AppError
from svetofor.db.portfolio import Portfolio, PortfolioItem, PortfolioOperation
from svetofor.portfolios.edit_access import ensure_idle, lock_items
from svetofor.portfolios.edit_capacity import capacity
from svetofor.portfolios.uploads import Source


def digest(kind: str, sources: list[Source], extra: object = None) -> str:
    value = [kind, [(s.source_type, s.source_name, s.source_sha256) for s in sources], extra]
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def previous_operation(
    db: Session, portfolio: Portfolio, key: str, fingerprint: str
) -> PortfolioOperation | None:
    previous = db.scalar(
        select(PortfolioOperation).where(
            PortfolioOperation.portfolio_id == portfolio.id, PortfolioOperation.request_key == key
        )
    )
    if previous and previous.request_hash != fingerprint:
        raise AppError("request_conflict", "Ключ запроса использован для другого изменения", 409)
    return previous


def retry_preparation(db: Session, op: PortfolioOperation, sources: list[Source]) -> bool:
    if op.status != "failed" or op.kind == "add":
        return False
    item = db.get(PortfolioItem, uuid.UUID(op.payload["item_id"]), with_for_update=True)
    if item is None:
        raise AppError("not_found", "Закупка удалена", 404)
    ensure_idle(db, item)
    if str(item.check_id) != str(op.payload["check_id"]) or item.version != op.payload["version"]:
        raise AppError("edit_conflict", "Закупка изменилась после ошибки. Обновите портфель", 409)
    portfolio = db.get(Portfolio, op.portfolio_id)
    if portfolio is None:
        raise AppError("not_found", "Портфель удалён", 404)
    entries = lock_items(db, portfolio.id)
    size = op.payload["reserved_bytes"]
    previous_stored = item.source_bytes if item.source_path or item.edit_token else 0
    capacity(
        db,
        portfolio,
        size - previous_stored,
        sum(i.source_bytes for i in entries) - item.source_bytes + size,
    )
    item.source_bytes = size
    op.payload = {**op.payload, "sources": [asdict(s) for s in sources]}
    op.status, op.error, op.started_at = "queued", None, None
    item.edit_token, item.edit_error = str(op.id), None
    return True


def operation(
    db: Session,
    portfolio: Portfolio,
    key: str,
    fingerprint: str,
    kind: str,
    payload: dict[str, Any],
) -> PortfolioOperation:
    value = PortfolioOperation(
        portfolio_id=portfolio.id,
        request_key=key,
        request_hash=fingerprint,
        kind=kind,
        payload=payload,
    )
    db.add(value)
    db.flush()
    return value
