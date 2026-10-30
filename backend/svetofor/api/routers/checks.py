"""Проверки пакетов: загрузка, ход, отчёт, перепроверка.

Обработчики не делают тяжёлую работу: файл сохраняется потоком,
а проверка уходит в очередь Celery. Ответ на загрузку — сразу, 202.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import func, or_, select

from svetofor.api.access import (
    EIS_SCOPE,
    check_for,
    only_visible,
    require_package_access,
    sign_task_id,
    task_id_for,
    visible_owner,
)
from svetofor.api.deps import CurrentUser, DbSession, SessionFactoryDep, user_for_events
from svetofor.api.errors import AppError, forbidden, not_found
from svetofor.api.security import TICKET_TTL_S, create_ticket
from svetofor.core.config import get_settings
from svetofor.core.ids import uuid7
from svetofor.core.logging import get_logger
from svetofor.db.enums import CheckStatus, FindingStatus, Light, Role
from svetofor.db.models import (
    AuditLog,
    Check,
    Finding,
    Package,
    Ruleset,
    RulesetVersion,
    User,
)
from svetofor.pipeline.finalize import fail_check, fail_stalled
from svetofor.pipeline.ingest import IngestConfig, create_check, new_check
from svetofor.pipeline.storage import PackagePaths
from svetofor.pipeline.unpack import ARCHIVE_EXTENSIONS, SUPPORTED_EXTENSIONS, repair_filename
from svetofor.reports.compare import compare_checks, previous_editions
from svetofor.reports.compare_docx import render_comparison_docx
from svetofor.reports.report import build_report, check_out
from svetofor.rules.store import latest_ruleset_version
from svetofor.schemas.checks import (
    CheckCreated,
    CheckListItem,
    CheckOut,
    CheckPage,
    ClarificationIn,
    EisIn,
    EisStatus,
    EisTask,
    ReportOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["Проверки"])
_CHUNK = 1024 * 1024
_TERMINAL = {CheckStatus.DONE, CheckStatus.DONE_WITH_WARNINGS, CheckStatus.FAILED}
_ACTIVE = {CheckStatus.QUEUED, CheckStatus.RUNNING}
_ACTIVE_WINDOW = timedelta(days=1)


def _limit_active_checks(db: DbSession, user_id: uuid.UUID) -> None:
    """Не больше N идущих проверок у пользователя — иначе один вход расходует модель без меры."""
    limit = get_settings().max_active_checks_per_user
    since = datetime.now(UTC) - _ACTIVE_WINDOW
    active = db.scalar(
        select(func.count(Check.id))
        .join(Package, Package.id == Check.package_id)
        .where(Package.owner_id == user_id, Check.status.in_(_ACTIVE), Check.created_at > since)
    )
    if (active or 0) >= limit:
        raise AppError(
            "too_many_checks",
            f"У вас уже идут проверки: {active}. Дождитесь, пока одна из них завершится",
            429,
        )


def _get_check(db: DbSession, check_id: uuid.UUID, user: User) -> Check:
    """Проверка, если пользователь вправе её видеть (специалист — только свои), иначе 404."""
    return check_for(db, check_id, user)


def _watch(db: DbSession, session_factory: Any, checks: list[Check]) -> None:
    """Сторож: активные проверки без движения дольше срока помечаются «прервалась»."""
    active = [c for c in checks if c.status in _ACTIVE]
    if active and fail_stalled(session_factory, [c.id for c in active]):
        for check in active:
            db.refresh(check)


def _ruleset_version_id(db: DbSession, code: str | None) -> uuid.UUID:
    version = latest_ruleset_version(db, code or get_settings().default_ruleset)
    if version is None:
        raise AppError(
            "ruleset_missing", "Набор правил не загружен. Обратитесь к администратору", 409
        )
    return version.id


async def _save_upload(upload: UploadFile, target: Path, limit: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with target.open("wb") as out:
            while block := await upload.read(_CHUNK):
                size += len(block)
                if size > limit:
                    raise AppError(
                        "too_large",
                        f"Файл больше {limit // (1024 * 1024)} МБ. Разделите пакет",
                        413,
                    )
                out.write(block)
    except BaseException:  # лимит, обрыв связи, отмена запроса — недогруженный файл не копим
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise AppError("empty_file", "Файл пустой", 400)


def _suffix(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def _check_format(name: str) -> None:
    if _suffix(name) not in ARCHIVE_EXTENSIONS | SUPPORTED_EXTENSIONS:
        raise AppError(
            "unsupported_format",
            f"Формат файла «{name or 'без имени'}» не поддерживается. Загрузите архив "
            "(ZIP, RAR, 7z) или документы PDF, DOC/DOCX, RTF, XLS/XLSX, JPG/PNG/TIFF",
            415,
        )


def _bundle_name(uploads: list[UploadFile]) -> str:
    count = len(uploads)
    form = "файла" if count % 10 == 1 and count % 100 != 11 else "файлов"
    return f"Пакет из {count} {form}.zip"


async def _save_bundle(uploads: list[UploadFile], target: Path, limit: int) -> None:
    """Несколько загруженных файлов — в один ZIP потоком, с общим лимитом размера."""
    target.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    size = 0
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as bundle:
            for upload in uploads:
                name = _unique(
                    PurePosixPath((upload.filename or "файл").replace("\\", "/")).name, used
                )
                with bundle.open(name, "w") as out:
                    while block := await upload.read(_CHUNK):
                        size += len(block)
                        if size > limit:
                            raise AppError(
                                "too_large",
                                f"Файлы вместе больше {limit // (1024 * 1024)} МБ. Разделите пакет",
                                413,
                            )
                        out.write(block)
    except BaseException:  # лимит, обрыв связи, отмена запроса — недогруженный архив не копим
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise AppError("empty_file", "Файлы пустые", 400)


def _unique(name: str, used: set[str]) -> str:
    """Одинаковые имена (из разных папок) — «ТЗ.pdf», «ТЗ (2).pdf»."""
    stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem} ({n}){suffix}"
    used.add(candidate.lower())
    return candidate


QUEUE_UNAVAILABLE = "Сервис проверки временно недоступен. Попробуйте позже"


def send_task(task: Any, *args: Any) -> Any:
    """Задача в очередь; брокер недоступен — 503 с понятной причиной, а не «внутренняя ошибка»."""
    try:
        return task.delay(*args)
    except Exception as exc:  # kombu/redis бросают разные ошибки
        log.exception("enqueue_failed", task=task.name)
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE, 503) from exc


def _enqueue(session_factory: Any, check_id: uuid.UUID) -> None:
    """Проверка в очередь. Очередь недоступна — проверка честно помечается «не удалась»."""
    from svetofor.tasks.checks import run_check

    try:
        run_check.delay(str(check_id))
    except Exception as exc:  # брокер недоступен: kombu/redis бросают разные ошибки
        log.exception("enqueue_failed", check_id=str(check_id))
        fail_check(session_factory, check_id, "Очередь задач недоступна, проверка не запущена")
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE, 503) from exc


@router.post(
    "/checks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CheckCreated,
    summary="Загрузить пакет и запустить проверку",
)
async def create(
    user: CurrentUser,
    session_factory: SessionFactoryDep,
    file: Annotated[UploadFile | None, File(description="Архив или документ")] = None,
    files: Annotated[
        list[UploadFile] | None,
        File(description="Несколько документов сразу — например, скачанные из ЕИС по одному"),
    ] = None,
    ruleset_code: Annotated[str | None, Form()] = None,
) -> CheckCreated:
    uploads = [u for u in [file, *(files or [])] if u is not None]
    if not uploads:
        raise AppError("no_file", "Выберите архив или документы для проверки", 400)
    for item in uploads:
        # curl под Windows присылает имя в cp1251 — без этого «Ïðîåêò» в истории и отчёте
        item.filename = repair_filename(item.filename) if item.filename else item.filename
        _check_format(item.filename or "")

    def check_limit() -> None:  # до приёма файла: отказ не должен стоить загрузки 100 МБ
        with session_factory() as db:
            _limit_active_checks(db, user.id)

    await run_in_threadpool(check_limit)
    settings = get_settings()
    if len(uploads) == 1:
        name = uploads[0].filename or "пакет"
        incoming = settings.packages_dir / "_incoming" / f"{uuid7()}{_suffix(name)}"
        await _save_upload(uploads[0], incoming, settings.max_upload_bytes)
    else:  # несколько файлов — в один ZIP, дальше обычный путь пакета
        name = _bundle_name(uploads)
        incoming = settings.packages_dir / "_incoming" / f"{uuid7()}.zip"
        await _save_bundle(uploads, incoming, settings.max_upload_bytes)

    def register() -> Check:
        with session_factory() as db:
            check = create_check(
                db,
                upload=incoming,
                original_name=name,
                config=IngestConfig.from_settings(settings),
                ruleset_version_id=_ruleset_version_id(db, ruleset_code),
                owner_id=user.id,
            )
            db.commit()
            return check

    try:
        check = await run_in_threadpool(register)
    finally:
        incoming.unlink(missing_ok=True)
    await run_in_threadpool(_enqueue, session_factory, check.id)
    return CheckCreated(package_id=check.package_id, check_id=check.id)


@router.post(
    "/checks/demo",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CheckCreated,
    summary="Проверить демо-пакет (работает без ключа модели)",
)
def demo(user: CurrentUser, db: DbSession, session_factory: SessionFactoryDep) -> CheckCreated:
    _limit_active_checks(db, user.id)
    settings = get_settings()
    source = settings.demo_package
    if not source.is_file():
        raise not_found("Демо-пакет")
    copy = settings.packages_dir / "_incoming" / f"{uuid7()}{source.suffix}"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(source.read_bytes())
    check = create_check(
        db,
        upload=copy,
        original_name=source.name,
        config=IngestConfig.from_settings(settings),
        ruleset_version_id=_ruleset_version_id(db, None),
        owner_id=user.id,
        is_demo=True,
    )
    db.commit()
    _enqueue(session_factory, check.id)
    return CheckCreated(package_id=check.package_id, check_id=check.id)


@router.post(
    "/checks/eis",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EisTask,
    summary="Загрузить закупку из ЕИС по реестровому номеру и проверить",
)
def from_eis(body: EisIn, user: CurrentUser, db: DbSession) -> EisTask:
    """Номер проверяется сразу, скачивание — задача воркера."""
    from svetofor.pipeline.eis import EisError, normalize_number
    from svetofor.tasks.eis import fetch_eis_task

    if not get_settings().eis_fetch:
        raise not_found("Загрузка из ЕИС")
    try:
        number = normalize_number(body.number)
    except EisError as exc:
        raise AppError("bad_number", str(exc), 400) from exc
    _limit_active_checks(db, user.id)
    version_id = _ruleset_version_id(db, None)
    task = send_task(fetch_eis_task, number, str(user.id), str(version_id))
    db.add(AuditLog(user_id=user.id, action="eis_fetch", entity="eis", entity_id=number))
    db.commit()
    return EisTask(task_id=sign_task_id(task.id, user.id, EIS_SCOPE))


@router.get("/checks/eis/{task_id}", response_model=EisStatus, summary="Ход загрузки из ЕИС")
def eis_status(task_id: str, user: CurrentUser) -> EisStatus:
    from celery.result import AsyncResult

    from svetofor.tasks.celery_app import celery_app

    result = AsyncResult(task_id_for(task_id, user, EIS_SCOPE), app=celery_app)
    if result.failed():
        return EisStatus(status="failed", error="Не удалось загрузить закупку из ЕИС")
    if not result.successful():
        return EisStatus(status="pending")
    value = result.result
    if value.get("error"):
        return EisStatus(status="failed", error=value["error"])
    return EisStatus(status="done", check_id=value["check_id"], package_id=value["package_id"])


@router.get("/checks", response_model=CheckPage, summary="История проверок")
def history(
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactoryDep,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    light: Light | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: Annotated[
        str | None, Query(max_length=200, description="Поиск по названию пакета и имени файла")
    ] = None,
) -> CheckPage:
    statement = only_visible(
        select(Check, Package.title).join(Package, Package.id == Check.package_id), user
    )
    if light is not None:
        statement = statement.where(Check.light == light)
    if q and q.strip():
        # % и _ из запроса — обычные символы, а не шаблон LIKE
        literal = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{literal}%"
        statement = statement.where(
            or_(
                Package.title.ilike(pattern, escape="\\"),
                Package.source_filename.ilike(pattern, escape="\\"),
            )
        )
    if date_from is not None:
        statement = statement.where(Check.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(Check.created_at <= date_to)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(Check.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    _watch(db, session_factory, [c for c, _ in rows])
    counts = dict(
        db.execute(
            select(Finding.check_id, func.count())
            .where(
                Finding.check_id.in_([c.id for c, _ in rows]),
                Finding.status == FindingStatus.SHOWN,
            )
            .group_by(Finding.check_id)
        ).all()
    )
    items = [
        CheckListItem(
            id=c.id,
            package_id=c.package_id,
            package_title=title,
            status=c.status,
            light=c.light,
            risk_index=c.risk_index,
            findings=counts.get(c.id, 0),
            created_at=c.created_at,
            finished_at=c.finished_at,
        )
        for c, title in rows
    ]
    return CheckPage(items=items, total=total, page=page, page_size=page_size)


@router.get("/checks/{check_id}", response_model=CheckOut, summary="Статус, этапы, светофор")
def get_check(
    check_id: uuid.UUID, user: CurrentUser, db: DbSession, session_factory: SessionFactoryDep
) -> CheckOut:
    check = _get_check(db, check_id, user)
    _watch(db, session_factory, [check])
    return check_out(db, check)


@router.get("/checks/{check_id}/report", response_model=ReportOut, summary="Полный отчёт")
def report(
    check_id: uuid.UUID, user: CurrentUser, db: DbSession, session_factory: SessionFactoryDep
) -> ReportOut:
    check = _get_check(db, check_id, user)
    _watch(db, session_factory, [check])
    if check.status not in _TERMINAL:
        raise AppError("not_ready", "Проверка ещё идёт — отчёт будет готов после завершения", 409)
    return build_report(db, check, user.id)


@router.get(
    "/checks/{check_id}/editions",
    summary="Прежние проверки той же закупки (по номеру извещения) — что можно сравнить",
)
def editions(check_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    return previous_editions(db, _get_check(db, check_id, user), owner_id=visible_owner(user))


@router.get(
    "/checks/{check_id}/compare",
    summary="Что изменилось относительно прежней редакции: пункты, карточка, риски",
)
def compare(
    check_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    with_check: Annotated[uuid.UUID, Query(alias="with", description="Проверка прежней редакции")],
) -> dict[str, Any]:
    new, old = _get_check(db, check_id, user), _get_check(db, with_check, user)
    if new.id == old.id:
        raise AppError("same_check", "Выберите другую проверку для сравнения", 400)
    if new.status not in _TERMINAL or old.status not in _TERMINAL:
        raise AppError("not_ready", "Обе проверки должны завершиться, чтобы их сравнить", 409)
    return compare_checks(db, old, new)


@router.get(
    "/checks/{check_id}/compare.docx",
    summary="Изменения относительно прежней редакции — в Word (было / стало)",
)
def compare_docx(
    check_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    with_check: Annotated[uuid.UUID, Query(alias="with", description="Проверка прежней редакции")],
) -> FileResponse:
    diff = compare(check_id, db, user, with_check)
    new, old = _get_check(db, check_id, user), _get_check(db, with_check, user)
    folder = PackagePaths(Path(new.package.storage_path)).reports
    title = f"Было: {old.package.title} ({old.created_at:%d.%m.%Y}). Стало: {new.package.title}."
    path = render_comparison_docx(diff, title, folder / f"compare_{old.id}.docx")
    return FileResponse(
        path,
        media_type=DOCX_MEDIA,
        filename=f"Светофор — изменения — {new.package.title}.docx",
    )


@router.post(
    "/checks/{check_id}/rerun",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CheckCreated,
    summary="Перепроверить пакет (например, после правки правил или типа документа)",
)
def rerun(
    check_id: uuid.UUID, user: CurrentUser, db: DbSession, session_factory: SessionFactoryDep
) -> CheckCreated:
    previous = _get_check(db, check_id, user)
    if previous.status not in _TERMINAL:
        raise AppError("busy", "Проверка ещё идёт — дождитесь завершения", 409)
    _limit_active_checks(db, user.id)
    # тот же набор правил, что у исходной проверки, в его последней версии (после правок)
    old_version = (
        db.get(RulesetVersion, previous.ruleset_version_id) if previous.ruleset_version_id else None
    )
    ruleset = db.get(Ruleset, old_version.ruleset_id) if old_version else None
    check = new_check(
        db, previous.package, _ruleset_version_id(db, ruleset.code if ruleset else None)
    )
    db.add(AuditLog(user_id=user.id, action="rerun", entity="check", entity_id=str(check.id)))
    db.commit()
    _enqueue(session_factory, check.id)
    return CheckCreated(package_id=check.package_id, check_id=check.id)


@router.delete(
    "/packages/{package_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Удалить пакет"
)
def delete_package(package_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    package = db.get(Package, package_id)
    if package is None:
        raise not_found("Пакет")
    require_package_access(user, package, "Пакет")  # чужой пакет специалисту — 404, не 403
    if package.owner_id != user.id and user.role is not Role.ADMIN:
        raise forbidden("Удалить пакет может только его владелец или администратор")
    busy = db.scalar(
        select(func.count())
        .select_from(Check)
        .where(Check.package_id == package_id, Check.status.in_(_ACTIVE))
    )
    if busy:  # воркеры заново создали бы каталоги удалённого пакета
        raise AppError("busy", "Пакет ещё проверяется — удалить можно после завершения", 409)
    paths = PackagePaths(Path(package.storage_path))
    db.delete(package)
    db.add(AuditLog(user_id=user.id, action="delete", entity="package", entity_id=str(package_id)))
    db.commit()
    paths.remove()


# ─────────────────────────── События (SSE) ───────────────────────────


def _require_visible(session_factory: Any, check_id: uuid.UUID, user: User) -> None:
    with session_factory() as db:
        check_for(db, check_id, user)


def _snapshot(session_factory: Any, check_id: uuid.UUID) -> dict[str, Any] | None:
    fail_stalled(session_factory, [check_id])
    with session_factory() as db:
        check = db.get(Check, check_id)
        return check_out(db, check).model_dump(mode="json") if check else None


@router.get(
    "/checks/{check_id}/events",
    summary="Ход проверки (Server-Sent Events: progress, done, error)",
    response_class=StreamingResponse,
)
async def events(
    check_id: uuid.UUID,
    request: Request,
    session_factory: SessionFactoryDep,
    user: Annotated[User, Depends(user_for_events)],
) -> StreamingResponse:
    # доступ — до начала потока: чужая проверка (с токеном в заголовке) — 404, как и несуществующая
    await run_in_threadpool(_require_visible, session_factory, check_id, user)

    async def stream() -> AsyncIterator[str]:
        last: dict[str, Any] | None = None
        while not await request.is_disconnected():
            snapshot = await run_in_threadpool(_snapshot, session_factory, check_id)
            if snapshot is None:
                yield _event("error", {"message": "Проверка не найдена"})
                return
            if snapshot != last:
                yield _event("progress", snapshot)
                last = snapshot
            if CheckStatus(snapshot["status"]) in _TERMINAL:
                yield _event("done", snapshot)
                return
            await asyncio.sleep(1)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # nginx не копит события
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.post(
    "/checks/{check_id}/events/ticket",
    summary="Билет на события проверки для EventSource (2 минуты, только эта проверка)",
)
def events_ticket(check_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    _get_check(db, check_id, user)
    ticket = create_ticket(user.id, user.role, f"sse:{check_id}")
    return {"ticket": ticket, "expires_in": TICKET_TTL_S}


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─────────────────────────── Выгрузка ───────────────────────────

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _finished(db: DbSession, check_id: uuid.UUID, user: User) -> Check:
    check = _get_check(db, check_id, user)
    if check.status not in _TERMINAL:
        raise AppError("not_ready", "Проверка ещё идёт — отчёт будет готов после завершения", 409)
    return check


@router.post("/checks/{check_id}/export", summary="Отчёт в Word (сразу) или PDF (готовится)")
def export(
    check_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    format: Annotated[Literal["docx", "pdf"], Query()] = "docx",
) -> Response:
    from svetofor.reports.export import download_name, report_paths, write_docx

    check = _finished(db, check_id, user)
    if format == "docx":
        return FileResponse(
            write_docx(db, check), media_type=DOCX_MEDIA, filename=download_name(check, ".docx")
        )
    pdf_path, error_path = report_paths(check)[1:]
    if pdf_path.is_file():
        return FileResponse(
            pdf_path, media_type="application/pdf", filename=download_name(check, ".pdf")
        )
    from svetofor.tasks.exports import export_pdf_task

    error_path.unlink(missing_ok=True)
    send_task(export_pdf_task, str(check_id))
    return JSONResponse(
        {"status": "preparing", "message": "PDF готовится, это несколько секунд"}, status_code=202
    )


@router.get("/checks/{check_id}/export/pdf", summary="Готовый PDF отчёта")
def export_pdf(check_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    from svetofor.reports.export import download_name, report_paths

    check = _finished(db, check_id, user)
    pdf_path, error_path = report_paths(check)[1:]
    if pdf_path.is_file():
        return FileResponse(
            pdf_path, media_type="application/pdf", filename=download_name(check, ".pdf")
        )
    if error_path.is_file():
        raise AppError("pdf_failed", error_path.read_text(encoding="utf-8"), 409)
    return JSONResponse({"status": "preparing", "message": "PDF ещё готовится"}, status_code=202)


@router.post("/checks/{check_id}/clarification", summary="Черновик запроса на разъяснение (Word)")
def clarification(
    check_id: uuid.UUID, body: ClarificationIn, user: CurrentUser, db: DbSession
) -> FileResponse:
    from svetofor.pipeline.orchestrator import check_rules
    from svetofor.pipeline.storage import PackagePaths
    from svetofor.reports.clarification import NothingToAskError, render_clarification_docx

    check = _finished(db, check_id, user)
    report_data = build_report(db, check, user.id)
    rules = {rule.id: rule for _, rule in check_rules(db, check)}
    target = (
        PackagePaths(Path(check.package.storage_path)).reports / f"{check.id}-clarification.docx"
    )
    selected = set(body.finding_ids) if body.finding_ids is not None else None
    try:
        render_clarification_docx(report_data, rules, target, selected)
    except NothingToAskError as exc:
        raise AppError("nothing_to_ask", str(exc), 409) from exc
    return FileResponse(
        target,
        media_type=DOCX_MEDIA,
        filename=f"Запрос на разъяснение — {check.package.title}.docx",
    )
