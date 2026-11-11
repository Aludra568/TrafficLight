"""Рабочий статус замечания: отдельный от вердикта правила и оценки цитаты."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import select, update

from svetofor.api.access import finding_for
from svetofor.api.deps import CurrentUser, DbSession
from svetofor.api.errors import AppError, not_found
from svetofor.db.enums import FindingStatus, HandlingStatus
from svetofor.db.models import AuditLog, Check, Finding
from svetofor.portfolios.snapshot import DONE
from svetofor.schemas.portfolios import HandlingIn, HandlingOut

router = APIRouter(prefix="/api/findings", tags=["Обработка замечаний"])


@router.patch("/{finding_id}/handling", response_model=HandlingOut)
def change_handling(
    finding_id: uuid.UUID,
    body: HandlingIn,
    user: CurrentUser,
    db: DbSession,
) -> HandlingOut:
    finding = finding_for(db, finding_id, user)
    if finding.status != FindingStatus.SHOWN:
        raise not_found("Замечание")
    check = db.scalar(
        select(Check)
        .where(Check.id == finding.check_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if check is None or check.status not in DONE:
        raise AppError("not_ready", "Дождитесь завершения проверки", 409)
    now = datetime.now(UTC)
    changed = db.execute(
        update(Finding)
        .where(
            Finding.id == finding_id,
            Finding.handling_version == body.version,
        )
        .values(
            handling_status=HandlingStatus(body.status),
            handling_version=body.version + 1,
            handling_updated_at=now,
            handling_updated_by=user.id,
        )
        .returning(Finding.id)
    ).scalar_one_or_none()
    if changed is None:
        raise AppError(
            "handling_conflict",
            "Статус уже изменён. Обновите отчёт и повторите действие",
            409,
        )
    # Новая редакция выгрузок: старый PDF не должен пережить смену рабочего статуса.
    counters = dict(check.counters)
    counters["handling_revision"] = int(counters.get("handling_revision", 0)) + 1
    check.counters = counters
    db.add(
        AuditLog(
            user_id=user.id,
            action="finding_handling",
            entity="finding",
            entity_id=str(finding_id),
        )
    )
    db.commit()
    return HandlingOut(status=body.status, version=body.version + 1, updated_at=now)
