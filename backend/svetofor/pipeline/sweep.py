"""Поштучная проверка пунктов перед итогом (`rules/sweep.py`, настройка GREEN_SWEEP).

Идёт после всех правил и до светофора: ей нужен итог пакета. Если в пакете уже есть критичное
замечание, она не нужна — красный она не изменит. Меняет только исходы правил «критично»:
обновляет строку исхода и добавляет новые замечания; прежние показанные замечания правила
удаляются, только если исход сменился (было «условие не найдено» — стало нарушение).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from svetofor.db.enums import FindingStatus, RuleOutcome, Severity
from svetofor.db.models import Check, Finding, RuleResult
from svetofor.pipeline.orchestrator import AnalysisDeps, check_rules, finding_row, load_context
from svetofor.rules.model import Rule
from svetofor.rules.results import RuleOutcomeDraft
from svetofor.rules.sweep import SweepDeps, SweepReport, sweep, sweep_targets
from svetofor.rules.types import EngineDeps
from svetofor.rules.types.llm_check import LlmCheckEvaluator

SessionFactory = Callable[[], Session]
FAILED_REASON = (
    "поштучная проверка не выполнена (внутренняя ошибка) — отсутствие риска не подтверждено"
)


def _drafts(rows: Sequence[RuleResult]) -> list[RuleOutcomeDraft]:
    return [RuleOutcomeDraft(r.rule_code, r.outcome, r.reason, (), r.details) for r in rows]


def _has_red(db: Session, check_id: uuid.UUID) -> bool:
    count = db.scalar(
        select(func.count())
        .select_from(Finding)
        .where(
            Finding.check_id == check_id,
            Finding.status == FindingStatus.SHOWN,
            Finding.severity == Severity.RED,
        )
    )
    return bool(count)


def _save(
    db: Session,
    check_id: uuid.UUID,
    versioned: Sequence[tuple[uuid.UUID, Rule]],
    rows: dict[str, RuleResult],
    outcomes: Sequence[RuleOutcomeDraft],
) -> None:
    by_code = {rule.id: (version_id, rule) for version_id, rule in versioned}
    for outcome in outcomes:
        row = rows.get(outcome.rule_id)
        if row is None or outcome.rule_id not in by_code:
            continue
        if (row.outcome, row.reason, row.details) == (
            outcome.outcome,
            outcome.reason,
            outcome.details,
        ) and not outcome.findings:
            continue
        if row.outcome is not outcome.outcome:
            db.execute(
                delete(Finding).where(
                    Finding.check_id == check_id,
                    Finding.rule_code == outcome.rule_id,
                    Finding.status == FindingStatus.SHOWN,
                )
            )
        row.outcome, row.reason, row.details = outcome.outcome, outcome.reason, outcome.details
        version_id, rule = by_code[outcome.rule_id]
        for draft in outcome.findings:
            db.add(finding_row(check_id, version_id, rule, draft))


def sweep_rules(
    session_factory: SessionFactory, check_id: uuid.UUID, deps: AnalysisDeps
) -> dict[str, Any] | None:
    """Поштучная проверка и запись исходов. Сводка — в `counters.sweep`; None — не выполнялась."""
    if deps.gateway is None:
        return None  # без модели смысловые правила и так «не проверены»
    # чтение — и сессия закрыта: вызовы модели идут минутами, держать транзакцию и соединение
    # пула всё это время незачем
    with session_factory() as db:
        check = db.get(Check, check_id)
        if check is None:
            return None
        red = _has_red(db, check_id)
        if not red:
            versioned = check_rules(db, check)
            drafts = _drafts(
                list(db.scalars(select(RuleResult).where(RuleResult.check_id == check_id)))
            )
            ctx = load_context(db, check)
    stats: dict[str, Any] = {"skipped": "red"}
    report: SweepReport | None = None
    if not red:
        evaluator = LlmCheckEvaluator(
            EngineDeps(
                verifier=deps.verifier,
                gateway=deps.gateway,
                check_id=check_id,
                whole_budget=deps.budgets.whole,
            )
        )
        report = sweep(
            drafts,
            [rule for _, rule in versioned],
            ctx,
            SweepDeps(deps.gateway, evaluator.recheck, check_id, deps.concurrency),
        )
        stats = report.stats
    with session_factory() as db:
        check = db.get(Check, check_id, with_for_update=True)
        if check is None:
            return None
        if report is not None:
            rows = {
                r.rule_code: r
                for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
            }
            _save(db, check_id, versioned, rows, report.outcomes)
        check.counters = {**check.counters, "sweep": stats}
        db.commit()
        return stats


def fail_sweep(session_factory: SessionFactory, check_id: uuid.UUID) -> None:
    """Сбой поштучной проверки — правила, которые она должна была подтвердить, «не проверены»:
    без неё «в порядке» по правилам «критично» не доказано (при включённой настройке)."""
    with session_factory() as db:
        check = db.get(Check, check_id)
        if check is None:
            return
        rows = list(db.scalars(select(RuleResult).where(RuleResult.check_id == check_id)))
        rules = [rule for _, rule in check_rules(db, check)]
        targets = {r.id for r in sweep_targets(rules, _drafts(rows))}
        for row in rows:
            if row.rule_code in targets and row.outcome is RuleOutcome.OK:
                details = dict(row.details or {})
                details.update(blocked="sweep_failed", was=row.outcome.value)
                row.outcome, row.reason = RuleOutcome.NOT_CHECKED, FAILED_REASON
                row.details = details
        check.counters = {**check.counters, "sweep": {"error": "internal"}}
        db.commit()
