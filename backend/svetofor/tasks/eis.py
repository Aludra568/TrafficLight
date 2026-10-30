"""Загрузка закупки из ЕИС по номеру: сеть — в воркере, не в API."""

from __future__ import annotations

import uuid
import zipfile
from typing import Any

from svetofor.core.ids import uuid7
from svetofor.core.logging import get_logger
from svetofor.pipeline.eis import EisError, fetch_package
from svetofor.pipeline.ingest import create_check
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import run_check, services

log = get_logger(__name__)


@celery_app.task(name="eis.fetch", soft_time_limit=300, time_limit=330)
def fetch_eis_task(number: str, user_id: str, ruleset_version_id: str) -> dict[str, Any]:
    """Скачать закупку, собрать пакет и поставить проверку в очередь.

    Ошибка ЕИС — не сбой задачи, а результат с причиной: её покажет интерфейс.
    """
    s = services()
    settings = s.settings
    try:
        package = fetch_package(
            number, max_files=settings.max_files, max_bytes=settings.max_upload_bytes
        )
    except EisError as exc:
        return {"error": str(exc)}
    incoming = settings.packages_dir / "_incoming" / f"{uuid7()}.zip"
    incoming.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(incoming, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in package.files:
                archive.writestr(name, data)
        with s.session_factory() as db:
            check = create_check(
                db,
                upload=incoming,
                original_name=f"Закупка № {package.number} ({package.law}).zip",
                config=s.ingest,
                ruleset_version_id=uuid.UUID(ruleset_version_id),
                owner_id=uuid.UUID(user_id),
            )
            db.commit()
            check_id, package_id = check.id, check.package_id
    finally:
        incoming.unlink(missing_ok=True)
    run_check.delay(str(check_id))
    log.info("eis_check_created", number=package.number, check_id=str(check_id))
    return {"check_id": str(check_id), "package_id": str(package_id)}
