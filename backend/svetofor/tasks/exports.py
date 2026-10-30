"""Выгрузка отчёта в PDF: LibreOffice работает секунды — не в обработчике HTTP."""

from __future__ import annotations

import uuid

from svetofor.core.logging import get_logger
from svetofor.db.models import Check
from svetofor.reports.export import report_paths, write_pdf
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import services

log = get_logger(__name__)


@celery_app.task(name="exports.pdf", soft_time_limit=180)
def export_pdf_task(check_id: str) -> str | None:
    s = services()
    with s.session_factory() as db:
        check = db.get(Check, uuid.UUID(check_id))
        if check is None:
            return None
        try:
            path = write_pdf(db, check, s.ingest.soffice)
        except Exception:
            log.exception("export_pdf_failed", check_id=check_id)
            report_paths(check)[2].write_text(
                "Не удалось подготовить PDF: внутренняя ошибка", encoding="utf-8"
            )
            return None
    return str(path) if path else None
