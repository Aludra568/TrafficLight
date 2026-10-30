"""Итог проверки и её жизненный цикл.

Подсветка каждой подтверждённой цитаты (у противоречий — в обоих документах), точный раздел
(самый глубокий пункт, где стоит цитата), светофор пакета, индекс риска, счётчики, статус.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.core.config import get_settings
from svetofor.core.metrics import CHECKS_TOTAL
from svetofor.core.versions import pipeline_snapshot
from svetofor.db.enums import (
    CheckStatus,
    DocType,
    DocumentStatus,
    FindingStatus,
    Light,
    RuleOutcome,
)
from svetofor.db.models import Check, Document, Finding, LLMCall, RuleResult, Section
from svetofor.llm.naming import public_model_name
from svetofor.llm.prompting import SYSTEM_TEMPLATE
from svetofor.pipeline.extract import extract_document
from svetofor.pipeline.highlight import Highlighter
from svetofor.pipeline.orchestrator import check_rules, load_context
from svetofor.pipeline.unpack import skip_hides_content
from svetofor.rules.aggregate import (
    can_be_red,
    critical_unchecked,
    light_from_severities,
    package_verdict,
)
from svetofor.rules.facts import FACTS_TEMPLATE
from svetofor.rules.model import RuleCategory
from svetofor.rules.results import BASIS_UNCONFIRMED, counts_as_assessed
from svetofor.rules.review import REVIEW_TEMPLATE
from svetofor.rules.risk_index import RiskModel, category_scores
from svetofor.rules.sweep import SWEEP_TEMPLATE
from svetofor.rules.types.llm_check import CHECK_TEMPLATE

SessionFactory = Callable[[], Session]


def _now() -> datetime:
    return datetime.now(UTC)


def start_check(session_factory: SessionFactory, check_id: uuid.UUID) -> bool:
    """Отметить начало. False — проверку запускать не надо: удалена или уже завершена.

    Завершённой она бывает, если сторож закрыл её, пока задача ждала в очереди: пользователь
    видит «прервалась» и мог уже перепроверить пакет — второй конвейер по тем же документам
    мешал бы первому. Повторная доставка задачи у идущей проверки (RUNNING) — продолжаем.
    """
    with session_factory() as db:
        check = db.get(Check, check_id, with_for_update=True)
        if check is None or check.status not in _ACTIVE:
            return False
        if check.status is CheckStatus.QUEUED:
            check.status, check.started_at = CheckStatus.RUNNING, _now()
            check.heartbeat_at = check.started_at
            db.commit()
        return True


def fail_check(session_factory: SessionFactory, check_id: uuid.UUID, message: str) -> None:
    with session_factory() as db:
        check = db.get(Check, check_id, with_for_update=True)
        if check is None or check.status not in _ACTIVE:
            return
        check.status, check.error, check.finished_at = CheckStatus.FAILED, message, _now()
        db.commit()
    CHECKS_TOTAL.labels(CheckStatus.FAILED.value).inc()


# С запасом: документ обрабатывается до 16 минут (OCR большого скана), а при нагрузке проверка
# ещё и ждёт в очереди. Сторож — для потерянных задач (перезапуск Redis), торопиться незачем
STALL_AFTER = timedelta(minutes=60)
STALLED_MESSAGE = (
    "Проверка прервалась: обработка не отвечала больше часа (возможно, перезапуск сервиса). "
    "Запустите проверку ещё раз"
)
_ACTIVE = (CheckStatus.QUEUED, CheckStatus.RUNNING)


def fail_stalled(
    session_factory: SessionFactory,
    check_ids: Sequence[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Сторож: проверки без движения дольше `STALL_AFTER` — «не удалась» с понятной причиной.

    Задача может пропасть без следа (воркер убит, Redis перезапущен — chord не соберётся),
    и тогда проверка навсегда осталась бы «в работе». Вызывается при чтении проверки и при
    старте воркера — отдельный планировщик не нужен.
    """
    moment = now or _now()
    statement = select(Check.id, Check.heartbeat_at, Check.started_at, Check.created_at).where(
        Check.status.in_(_ACTIVE)
    )
    if check_ids is not None:
        statement = statement.where(Check.id.in_(list(check_ids)))
    with session_factory() as db:
        rows = list(db.execute(statement))
    stalled = [
        row.id
        for row in rows
        if moment - (row.heartbeat_at or row.started_at or row.created_at) > STALL_AFTER
    ]
    for check_id in stalled:
        fail_check(session_factory, check_id, STALLED_MESSAGE)
    return stalled


class _Highlights:
    """Один разбор PDF на документ, сколько бы цитат в нём ни было."""

    def __init__(self, db: Session, max_pages: int) -> None:
        self._db = db
        self._max_pages = max_pages
        self._open: dict[str, Highlighter | None] = {}

    def locate(self, document_id: str, start: int, end: int) -> list[dict[str, Any]]:
        if document_id not in self._open:
            document = self._db.get(Document, uuid.UUID(document_id))
            pdf = Path(document.pdf_path) if document and document.pdf_path else None
            self._open[document_id] = (
                Highlighter(pdf, extract_document(pdf, self._max_pages))
                if pdf and pdf.is_file()
                else None
            )
        highlighter = self._open[document_id]
        if highlighter is None:
            return []
        return [{"document_id": document_id, **h.as_dict()} for h in highlighter.locate(start, end)]

    def close(self) -> None:
        for highlighter in self._open.values():
            if highlighter is not None:
                highlighter.close()


def deepest_section(db: Session, document_id: uuid.UUID, offset: int) -> str | None:
    """Путь самого глубокого раздела или пункта, в который попадает смещение."""
    return db.scalar(
        select(Section.path)
        .where(
            Section.document_id == document_id,
            Section.char_start <= offset,
            Section.char_end > offset,
        )
        .order_by(Section.level.desc())
        .limit(1)
    )


def _place_findings(db: Session, findings: list[Finding], max_pages: int) -> None:
    highlights = _Highlights(db, max_pages)
    try:
        for finding in findings:
            evidence = [dict(item) for item in (finding.related or {}).get("evidence", [])]
            boxes: list[dict[str, Any]] = []
            for item in evidence:
                own = highlights.locate(item["document_id"], item["char_start"], item["char_end"])
                boxes.extend(own)
                # место каждой цитаты — до самого глубокого пункта и по реальным страницам
                path = deepest_section(db, uuid.UUID(item["document_id"]), item["char_start"])
                item["section_path"] = path or item["section_path"]
                if own:
                    item["page_from"] = min(b["page"] for b in own)
                    item["page_to"] = max(b["page"] for b in own)
            finding.highlights = boxes
            if not evidence:
                continue
            finding.related = {**(finding.related or {}), "evidence": evidence}
            main = evidence[0]
            finding.section_path = main["section_path"]
            finding.page_from, finding.page_to = main["page_from"], main["page_to"]
    finally:
        highlights.close()


PROMPT_VERSIONS = {
    "system": SYSTEM_TEMPLATE,
    "check_group": CHECK_TEMPLATE,
    "facts": FACTS_TEMPLATE,
    "doc_type": "doc_type.v1",
    "review": REVIEW_TEMPLATE,
    "sweep": SWEEP_TEMPLATE,
}


def _record_models(db: Session, check: Check) -> None:
    """Какими моделями и промптами получен результат — для отчёта и воспроизводимости."""
    calls = list(db.scalars(select(LLMCall).where(LLMCall.check_id == check.id)))
    main = Counter(c.model for c in calls if c.task != "doc_type")
    fast = Counter(c.model for c in calls if c.task == "doc_type")
    # в проверку (и в отчёт) — имя без адреса облака; полное остаётся в журнале llm_calls
    check.llm_model_main = public_model_name(main.most_common(1)[0][0]) if main else None
    check.llm_model_fast = public_model_name(fast.most_common(1)[0][0]) if fast else None
    check.prompt_versions = PROMPT_VERSIONS if calls else {}


def incomplete_documents(documents: Sequence[Document], relevant: set[DocType]) -> list[str]:
    """Документы, текст которых прочитан не полностью, — «рисков нет» по ним не доказано.

    Не открылся; пропущен при распаковке, хотя мог содержать условия (пароль, повреждение,
    незнакомый формат); или в документе нужного правилам типа есть нераспознанные страницы.
    """
    result = []
    for document in documents:
        unreadable = (document.signals or {}).get("unreadable_pages")
        if (
            document.status is DocumentStatus.FAILED
            or (
                document.status is DocumentStatus.SKIPPED
                and skip_hides_content(document.relative_path, document.error)
            )
            or (
                document.status is DocumentStatus.READY
                and unreadable
                and document.doc_type in relevant
            )
        ):
            result.append(document.filename)
    return result


def finalize_check(
    session_factory: SessionFactory, check_id: uuid.UUID, max_pages: int = 1000
) -> CheckStatus:
    with session_factory() as db:
        check = db.get(Check, check_id, with_for_update=True)
        if check is None:
            return CheckStatus.FAILED
        findings = list(db.scalars(select(Finding).where(Finding.check_id == check_id)))
        shown = [f for f in findings if f.status is FindingStatus.SHOWN]
        _place_findings(db, shown, max_pages)

        documents = list(
            db.scalars(select(Document).where(Document.package_id == check.package_id))
        )
        ready = [d for d in documents if d.status is DocumentStatus.READY]
        document_lights = [
            light_from_severities(f.severity for f in shown if f.document_id == d.id) for d in ready
        ]
        package_level = [f.severity for f in shown if f.document_id is None]
        results = list(db.scalars(select(RuleResult).where(RuleResult.check_id == check_id)))
        outcomes = Counter(r.outcome.value for r in results)
        # в полноту зелёного — только исходы по существу: «в порядке» без цитаты не доказывает
        assessed = sum(1 for r in results if counts_as_assessed(r.outcome, r.details))
        # правила другого закона к закупке не относятся — в знаменатель полноты не входят
        applicable = sum(1 for r in results if not (r.details or {}).get("other_law"))
        # правила, которые могли дать «критично», но не проверены из-за сбоя
        rules = [rule for _, rule in check_rules(db, check)]
        red_capable = {rule.id for rule in rules if can_be_red(rule)}
        critical = critical_unchecked(
            ((r.rule_code, r.outcome, r.details) for r in results), red_capable
        )
        incomplete = incomplete_documents(documents, {t for r in rules for t in r.applies_to})
        check.light = package_verdict(
            document_lights, package_level, assessed, applicable, len(critical), len(incomplete)
        )

        scores = category_scores([(f.category, f.severity) for f in shown])
        # индекс без проверки — ложное «можно участвовать», поэтому у серого пакета его нет
        check.risk_index = (
            None
            if check.light is Light.GRAY
            else RiskModel.uniform([c.value for c in RuleCategory]).index(scores)
        )
        check.law_detected = load_context(db, check).law
        _record_models(db, check)
        check.pipeline = pipeline_snapshot(get_settings())

        # «не проверено» из-за сбоя модели или карточки: зелёный цвет тогда неполный
        rules_failed = sum(1 for r in results if (r.details or {}).get("error"))
        failed_documents = sum(1 for d in documents if d.status is DocumentStatus.FAILED)
        check.counters = {
            **check.counters,
            "findings": len(shown),
            "rejected_unverified": len(findings) - len(shown),
            "rules": dict(outcomes),
            "rules_failed": rules_failed,
            # виды сбоев: таймаут выражения и неподтверждённая цитата не лечатся повтором
            "rules_errors": dict(
                Counter(
                    str((r.details or {})["error"])
                    for r in results
                    if (r.details or {}).get("error")
                )
            ),
            "critical_unchecked": critical,
            # «в порядке» модели без подтверждённой цитаты; «не проверено» из-за нечитаемого
            # текста или указаний программе проверки в документе
            "rules_unconfirmed": sum(
                1
                for r in results
                if r.outcome is RuleOutcome.OK
                and (r.details or {}).get("basis") == BASIS_UNCONFIRMED
            ),
            "rules_blocked": dict(
                Counter(
                    str((r.details or {})["blocked"])
                    for r in results
                    if (r.details or {}).get("blocked")
                )
            ),
            "documents_failed": failed_documents,
            # документы, прочитанные не полностью: зелёный по пакету невозможен
            "documents_incomplete": incomplete,
        }
        warnings = (
            failed_documents
            or rules_failed
            or check.counters.get("facts_errors")
            or not ready
            or check.light is Light.GRAY
        )
        check.status = CheckStatus.DONE_WITH_WARNINGS if warnings else CheckStatus.DONE
        if check.error == STALLED_MESSAGE:  # сторож поторопился: работа всё же завершилась
            check.error = None
        check.finished_at = _now()
        status = check.status
        db.commit()
    CHECKS_TOTAL.labels(status.value).inc()
    return status
