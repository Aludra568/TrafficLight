"""Изменение названия и отдельных позиций портфеля."""

import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from svetofor.api.deps import CurrentUser, DbSession, SessionFactoryDep
from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import User
from svetofor.portfolios.dispatch import enqueue
from svetofor.portfolios.edit_access import (
    check_version,
    editable_portfolio,
    ensure_idle,
    find_item,
    lock_items,
    lock_portfolio,
)
from svetofor.portfolios.editing import add_sources
from svetofor.portfolios.snapshot import snapshot
from svetofor.portfolios.uploads import file_sources, number_sources
from svetofor.schemas.portfolios import PortfolioCreated, PortfolioOut

router = APIRouter(prefix="/api/portfolios", tags=["Редактирование портфеля"])


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=0)


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
def rename(
    portfolio_id: uuid.UUID, body: RenameIn, user: CurrentUser, db: DbSession
) -> PortfolioOut:
    portfolio = lock_portfolio(db, portfolio_id, user)
    if portfolio.title_version != body.version:
        raise AppError("edit_conflict", "Название изменено. Обновите портфель", 409)
    title = body.title.strip()
    if not title:
        raise AppError("empty_title", "Укажите название портфеля", 400)
    portfolio.title, portfolio.title_version = title, portfolio.title_version + 1
    db.commit()
    result = snapshot(db, portfolio)
    result.can_edit = True
    return result


def check_access(factory: Callable[[], Session], identity: uuid.UUID, user: User) -> None:
    with factory() as db:
        editable_portfolio(db, identity, user)


def dispatch_edit(factory: Callable[[], Session], identity: uuid.UUID) -> str | None:
    from svetofor.tasks.portfolio_edits import prepare_edit

    try:
        prepare_edit.delay(str(identity))
    except Exception:
        return "Очередь недоступна. Изменение сохранено и будет поставлено повторно"
    return None


@router.post("/{portfolio_id}/items", response_model=PortfolioCreated, status_code=202)
async def append_items(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    factory: SessionFactoryDep,
    request_key: Annotated[uuid.UUID, Form()],
    files: Annotated[list[UploadFile] | None, File()] = None,
    numbers: Annotated[str | None, Form(max_length=5000)] = None,
    bundle: Annotated[bool, Form()] = False,
) -> PortfolioCreated:
    await run_in_threadpool(check_access, factory, portfolio_id, user)
    if bool(files) == bool(numbers and numbers.strip()) or (bundle and not files):
        raise AppError("source_mode", "Выберите документы или номера закупок", 400)
    folder = get_settings().data_dir / "portfolio_uploads" / str(uuid.uuid4())
    sources = await file_sources(files, folder) if files else number_sources(numbers or "")

    def save() -> tuple[uuid.UUID, bool, str]:
        with factory() as db:
            portfolio = lock_portfolio(db, portfolio_id, user)
            op, fresh = add_sources(db, portfolio, str(request_key), sources, bundle)
            db.commit()
            return op.id, fresh, op.kind

    try:
        op_id, fresh, kind = await run_in_threadpool(save)
    except BaseException:
        if folder.exists():
            shutil.rmtree(folder)
        raise
    if not fresh and folder.exists():
        shutil.rmtree(folder)
    error = None
    if fresh:
        if kind == "bundle":
            error = await run_in_threadpool(dispatch_edit, factory, op_id)
        else:
            error = await run_in_threadpool(enqueue, portfolio_id, factory)
    return PortfolioCreated(id=portfolio_id, dispatch_error=error)


@router.delete("/{portfolio_id}/items/{item_id}", status_code=204)
def remove_item(
    portfolio_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    version: Annotated[int, Query(ge=0)],
) -> Response:
    portfolio = lock_portfolio(db, portfolio_id, user)
    item = find_item(lock_items(db, portfolio.id), item_id)
    check_version(item, version)
    ensure_idle(db, item)
    source = Path(item.source_path).resolve() if item.source_path else None
    db.delete(item)
    db.commit()
    root = (get_settings().data_dir / "portfolio_uploads").resolve()
    if source and source.is_relative_to(root):
        source.unlink(missing_ok=True)
    return Response(status_code=204)
