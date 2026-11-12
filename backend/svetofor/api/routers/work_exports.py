"""Выгрузки из согласованного снимка БД, без общих временных файлов."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from svetofor.api.access import check_for
from svetofor.api.deps import CurrentUser, SessionFactoryDep
from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.db.models import Check
from svetofor.portfolios.snapshot import TERMINAL, portfolio_for, snapshot
from svetofor.reports.portfolio_docx import render_portfolio_docx
from svetofor.reports.report import build_report
from svetofor.reports.xlsx import render_xlsx

router = APIRouter(prefix="/api", tags=["Рабочие выгрузки"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def attachment(content: bytes, name: str, media: str) -> Response:
    return Response(
        content,
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/checks/{check_id}/export.xlsx", summary="Рабочий чек-лист в Excel")
def check_xlsx(check_id: uuid.UUID, user: CurrentUser, factory: SessionFactoryDep) -> Response:
    with factory() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        check = check_for(db, check_id, user)
        if check.status not in TERMINAL:
            raise AppError("not_ready", "Дождитесь завершения проверки", 409)
        report = build_report(db, check, user.id)
    content = render_xlsx([report], report.package.title, get_settings().public_app_url)
    return attachment(content, "Светофор — чек-лист.xlsx", XLSX)


@router.get("/portfolios/{portfolio_id}/export.xlsx", summary="Чек-лист портфеля в Excel")
def portfolio_xlsx(
    portfolio_id: uuid.UUID, user: CurrentUser, factory: SessionFactoryDep
) -> Response:
    with factory() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        portfolio = portfolio_for(db, portfolio_id, user)
        data = snapshot(db, portfolio)
        reports = []
        for item in data.items:
            if item.status in {"done", "failed"} and item.check_id:
                check = db.get(Check, item.check_id)
                if check is not None:
                    reports.append(build_report(db, check, user.id))
    content = render_xlsx(reports, data.title, get_settings().public_app_url, data)
    return attachment(content, "Светофор — портфель.xlsx", XLSX)


@router.get("/portfolios/{portfolio_id}/export.docx", summary="Сводный отчёт портфеля в Word")
def portfolio_docx(
    portfolio_id: uuid.UUID, user: CurrentUser, factory: SessionFactoryDep
) -> Response:
    with factory() as db:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        data = snapshot(db, portfolio_for(db, portfolio_id, user))
    content = render_portfolio_docx(data, get_settings().public_app_url)
    return attachment(content, "Светофор — портфель.docx", DOCX)
