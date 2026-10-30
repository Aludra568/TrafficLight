"""Ход проверки по этапам.

Каждая запись — отдельная короткая транзакция: пользователь видит прогресс сразу, а не
после завершения задачи. Время этапа попадает в `checks.timings` и в метрику Prometheus.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from svetofor.core.metrics import STAGE_SECONDS
from svetofor.db.enums import Stage, StageStatus
from svetofor.db.models import Check, CheckStage


def init_stages(db: Session, check_id: uuid.UUID) -> None:
    """Все этапы проверки в состоянии «ожидает» (повторный вызов ничего не дублирует)."""
    existing = set(db.scalars(select(CheckStage.stage).where(CheckStage.check_id == check_id)))
    for index, stage in enumerate(Stage):
        if stage.value not in existing:
            db.add(CheckStage(check_id=check_id, stage=stage.value, order_index=index))
    db.flush()


def _now() -> datetime:
    return datetime.now(UTC)


class StageTracker:
    def __init__(self, session_factory: Callable[[], Session], check_id: uuid.UUID) -> None:
        self._session_factory = session_factory
        self.check_id = check_id

    def _update(self, stage: Stage, **values: object) -> None:
        with self._session_factory() as db:
            row = db.scalar(
                select(CheckStage).where(
                    CheckStage.check_id == self.check_id, CheckStage.stage == stage.value
                )
            )
            if row is None:
                return
            finished = values.get("status") in (StageStatus.DONE, StageStatus.SKIPPED)
            if values.get("status") is StageStatus.RUNNING and row.started_at is None:
                row.started_at = _now()
            for key, value in values.items():
                setattr(row, key, value)
            # «пульс» проверки для сторожа зависших проверок (finalize.fail_stalled)
            db.execute(update(Check).where(Check.id == self.check_id).values(heartbeat_at=_now()))
            if finished or values.get("status") is StageStatus.FAILED:
                row.finished_at = _now()
                if row.started_at is not None and finished:
                    seconds = (row.finished_at - row.started_at).total_seconds()
                    STAGE_SECONDS.labels(stage.value).observe(seconds)
                    check = db.get(Check, self.check_id, with_for_update=True)
                    if check is not None:
                        check.timings = {**check.timings, stage.value: round(seconds, 2)}
            db.commit()

    def start(self, stage: Stage, detail: str | None = None) -> None:
        self._update(stage, status=StageStatus.RUNNING, detail=detail)

    def progress(self, stage: Stage, percent: int, detail: str | None = None) -> None:
        self._update(
            stage, status=StageStatus.RUNNING, progress=max(0, min(100, percent)), detail=detail
        )

    def done(self, stage: Stage, detail: str | None = None) -> None:
        self._update(stage, status=StageStatus.DONE, progress=100, detail=detail)

    def skip(self, stage: Stage, detail: str | None = None) -> None:
        self._update(stage, status=StageStatus.SKIPPED, progress=100, detail=detail)

    def fail(self, stage: Stage, detail: str) -> None:
        self._update(stage, status=StageStatus.FAILED, detail=detail)
