"""Состав документов позиции и асинхронная новая редакция пакета."""

import shutil
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select

from svetofor.api.deps import CurrentUser, DbSession, SessionFactoryDep
from svetofor.api.errors import AppError
from svetofor.api.routers.portfolio_editing import check_access, dispatch_edit
from svetofor.core.config import get_settings
from svetofor.db.models import Check, Document
from svetofor.db.portfolio import PortfolioItem
from svetofor.portfolios.edit_access import editable_portfolio, ensure_idle, lock_portfolio
from svetofor.portfolios.editing import begin_documents
from svetofor.portfolios.uploads import file_sources
from svetofor.schemas.portfolios import PortfolioCreated

router = APIRouter(prefix="/api/portfolios", tags=["Редактирование портфеля"])


class EditDocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: str


class DocumentsOut(BaseModel):
    item_version: int
    editable: bool
    documents: list[EditDocumentOut]


@router.get("/{portfolio_id}/items/{item_id}/documents", response_model=DocumentsOut)
def documents(
    portfolio_id: uuid.UUID, item_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> DocumentsOut:
    editable_portfolio(db, portfolio_id, user)
    item = db.get(PortfolioItem, item_id)
    if item is None or item.portfolio_id != portfolio_id:
        raise AppError("not_found", "Закупка не найдена", 404)
    ensure_idle(db, item, documents=True)
    check = db.get(Check, item.check_id)
    if check is None:
        raise AppError("not_found", "Проверка удалена", 404)
    rows = db.scalars(
        select(Document)
        .where(Document.package_id == check.package_id)
        .order_by(Document.relative_path, Document.id)
    )
    return DocumentsOut(
        item_version=item.version,
        editable=True,
        documents=[
            EditDocumentOut(id=d.id, filename=d.filename, status=d.status.value) for d in rows
        ],
    )


@router.post(
    "/{portfolio_id}/items/{item_id}/documents", response_model=PortfolioCreated, status_code=202
)
async def change_documents(
    portfolio_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    factory: SessionFactoryDep,
    request_key: Annotated[uuid.UUID, Form()],
    version: Annotated[int, Form(ge=0)],
    remove_ids: Annotated[str, Form(max_length=20000)] = "[]",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> PortfolioCreated:
    await run_in_threadpool(check_access, factory, portfolio_id, user)
    try:
        remove = TypeAdapter(list[uuid.UUID]).validate_json(remove_ids)
    except ValidationError as exc:
        raise AppError("bad_documents", "Некорректный список документов", 400) from exc
    folder = get_settings().data_dir / "portfolio_uploads" / str(uuid.uuid4())
    sources = await file_sources(files, folder) if files else []

    def save() -> tuple[uuid.UUID, bool]:
        with factory() as db:
            portfolio = lock_portfolio(db, portfolio_id, user)
            op, fresh = begin_documents(
                db, portfolio, item_id, str(request_key), version, remove, sources
            )
            db.commit()
            return op.id, fresh

    try:
        op_id, fresh = await run_in_threadpool(save)
    except BaseException:
        if folder.exists():
            shutil.rmtree(folder)
        raise
    if not fresh and folder.exists():
        shutil.rmtree(folder)
    error = await run_in_threadpool(dispatch_edit, factory, op_id) if fresh else None
    return PortfolioCreated(id=portfolio_id, dispatch_error=error)
