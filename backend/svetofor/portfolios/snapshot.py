"""Чтение портфеля: доступ и сводка по конкретным запускам проверок."""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.access import sees_all_packages
from svetofor.api.errors import not_found
from svetofor.db.enums import CheckStatus, FindingStatus
from svetofor.db.models import Check, Finding, User
from svetofor.db.portfolio import Portfolio
from svetofor.portfolios.summary import counts, overall_light, repeated_risks
from svetofor.reports.report import check_out
from svetofor.rules.model import CATEGORY_TITLES, RuleCategory
from svetofor.schemas.portfolios import PortfolioFinding, PortfolioItemOut, PortfolioOut

DONE = {CheckStatus.DONE, CheckStatus.DONE_WITH_WARNINGS}
ACTIVE = {CheckStatus.QUEUED, CheckStatus.RUNNING}


def portfolio_for(db: Session, portfolio_id: uuid.UUID, user: User) -> Portfolio:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None or (portfolio.owner_id != user.id and not sees_all_packages(user)):
        raise not_found("Портфель")
    return portfolio


def snapshot(db: Session, portfolio: Portfolio) -> PortfolioOut:
    items: list[PortfolioItemOut] = []
    findings: list[PortfolioFinding] = []
    for entry in portfolio.items:
        check = db.get(Check, entry.check_id) if entry.check_id else None
        if check and check.status == CheckStatus.FAILED and entry.status in {"waiting", "starting"}:
            check = None
        state = "waiting"
        error = entry.error
        progress = 0
        light = None
        if check is not None:
            state = "done" if check.status in DONE else "running"
            if check.status == CheckStatus.FAILED:
                state, error = "failed", check.error or "Проверка завершилась с ошибкой"
            light = check.light if state == "done" else None
            stages = check_out(db, check).stages
            progress = round(sum(s.progress for s in stages) / len(stages)) if stages else 0
            if state == "done":
                progress = 100
        elif entry.status == "checking":
            state, error = "failed", "Пакет или проверка были удалены"
        elif entry.status == "failed":
            state = "failed"
        elif entry.status == "starting":
            state = "running"
        own = []
        if check is not None and check.status in DONE:
            own = list(
                db.scalars(
                    select(Finding).where(
                        Finding.check_id == check.id,
                        Finding.status == FindingStatus.SHOWN,
                    )
                )
            )
            findings.extend(
                PortfolioFinding(
                    id=f.id,
                    check_id=check.id,
                    source_name=entry.source_name,
                    rule_code=f.rule_code,
                    title=f.title,
                    severity=f.severity.value,
                    category=f.category,
                    category_title=(
                        CATEGORY_TITLES.get(RuleCategory(f.category), f.category)
                        if f.category in RuleCategory
                        else f.category
                    ),
                    quote=f.quote,
                    document_id=f.document_id,
                    page=f.page_from,
                    handling_status=f.handling_status.value,
                )
                for f in own
            )
        items.append(
            PortfolioItemOut(
                id=entry.id,
                position=entry.position,
                source_type=entry.source_type,
                source_name=entry.source_name,
                check_id=entry.check_id,
                status=state,
                progress=progress,
                light=light,
                error=error,
                findings=len(own),
            )
        )
    totals = counts(items)
    findings.sort(key=lambda f: (f.severity != "red", f.rule_code, f.source_name, str(f.id)))
    return PortfolioOut(
        id=portfolio.id,
        title=portfolio.title,
        created_at=portfolio.created_at,
        light=overall_light(items),
        complete=not (totals["waiting"] or totals["running"]),
        total=len(items),
        **totals,
        distribution=dict(Counter(i.light.value for i in items if i.light is not None)),
        items=items,
        repeated_risks=repeated_risks(findings),
        findings=findings,
    )
