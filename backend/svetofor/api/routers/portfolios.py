"""Портфели: приём отдельных закупок, сводка и повтор неуспешных элементов."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from svetofor.api.access import sees_all_packages
from svetofor.api.deps import CurrentUser, DbSession, SessionFactoryDep
from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import Check
from svetofor.db.portfolio import Portfolio
from svetofor.portfolios.admission import ensure_capacity
from svetofor.portfolios.dispatch import enqueue
from svetofor.portfolios.register import register
from svetofor.portfolios.snapshot import portfolio_for, snapshot
from svetofor.portfolios.uploads import file_sources, number_sources, validate_count
from svetofor.schemas.portfolios import (
    PortfolioBrief,
    PortfolioCreated,
    PortfolioList,
    PortfolioOut,
)

router = APIRouter(prefix="/api/portfolios", tags=["Портфель"])


@router.post("", response_model=PortfolioCreated, status_code=202)
async def create_portfolio(
    user: CurrentUser,
    session_factory: SessionFactoryDep,
    title: Annotated[str, Form(min_length=1, max_length=200)],
    request_key: Annotated[uuid.UUID, Form()],
    files: Annotated[list[UploadFile] | None, File()] = None,
    numbers: Annotated[str | None, Form(max_length=5000)] = None,
) -> PortfolioCreated:
    title = title.strip()
    if not title:
        raise AppError("empty_title", "Укажите название портфеля", 400)
    if bool(files) == bool(numbers and numbers.strip()):
        raise AppError("source_mode", "Выберите файлы или список номеров ЕИС", 400)
    folder = get_settings().data_dir / "portfolio_uploads" / str(uuid.uuid4())
    sources = await file_sources(files, folder) if files else number_sources(numbers or "")
    validate_count(len(sources))

    def save() -> tuple[uuid.UUID, bool]:
        with session_factory() as db:
            portfolio, fresh = register(db, user.id, title, str(request_key), sources)
            db.commit()
            return portfolio.id, fresh

    try:
        portfolio_id, fresh = await run_in_threadpool(save)
    except BaseException:
        if folder.exists():
            shutil.rmtree(folder)
        raise
    if not fresh and folder.exists():
        shutil.rmtree(folder)
    error = await run_in_threadpool(enqueue, portfolio_id, session_factory)
    return PortfolioCreated(id=portfolio_id, dispatch_error=error)


@router.get("", response_model=PortfolioList)
def list_portfolios(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PortfolioList:
    query = select(Portfolio)
    if not sees_all_packages(user):
        query = query.where(Portfolio.owner_id == user.id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(Portfolio.created_at.desc(), Portfolio.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return PortfolioList(
        total=total,
        items=[
            PortfolioBrief(
                id=p.id,
                title=p.title,
                created_at=p.created_at,
                total=len(p.items),
            )
            for p in rows
        ],
    )


@router.get("/{portfolio_id}", response_model=PortfolioOut)
def get_portfolio(portfolio_id: uuid.UUID, user: CurrentUser, db: DbSession) -> PortfolioOut:
    return snapshot(db, portfolio_for(db, portfolio_id, user))


@router.post("/{portfolio_id}/retry", response_model=PortfolioCreated, status_code=202)
def retry_portfolio(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactoryDep,
) -> PortfolioCreated:
    from svetofor.db.enums import CheckStatus

    portfolio = portfolio_for(db, portfolio_id, user)
    owner = portfolio.owner_id
    if owner is None:
        raise AppError("owner_missing", "Владелец портфеля удалён; создайте новый портфель", 409)
    # Совпадает с порядком блокировок диспетчера: владелец, затем элементы.
    from svetofor.db.models import User
    from svetofor.db.portfolio import PortfolioItem

    db.execute(select(User.id).where(User.id == portfolio.owner_id).with_for_update())
    entries = list(
        db.scalars(
            select(PortfolioItem)
            .where(
                PortfolioItem.portfolio_id == portfolio_id,
            )
            .with_for_update()
        )
    )
    busy = any(
        entry.status in {"waiting", "starting"}
        or (
            entry.check_id is not None
            and (old := db.get(Check, entry.check_id)) is not None
            and old.status in {CheckStatus.QUEUED, CheckStatus.RUNNING}
        )
        for entry in entries
    )
    if not busy:
        ensure_capacity(db, owner, 0)
    for entry in entries:
        check = db.get(Check, entry.check_id) if entry.check_id else None
        if entry.status == "failed" or (check and check.status == CheckStatus.FAILED):
            if (
                check is None
                and entry.source_type == "file"
                and not Path(entry.source_path or "").is_file()
            ):
                entry.status, entry.error = (
                    "failed",
                    "Исходный файл удалён; загрузите новый портфель",
                )
                continue
            entry.status, entry.error, entry.started_at = "waiting", None, None
    db.commit()
    return PortfolioCreated(id=portfolio_id, dispatch_error=enqueue(portfolio_id, session_factory))
