"""Отчёт по проверке — один источник для API, интерфейса и выгрузки в DOCX/PDF.

Карточка закупки, документы со светофором, подтверждённые замечания (с нормой закона,
объяснением и рекомендацией), результат по каждому правилу («что проверено»), индекс риска.
Замечания, отклонённые проверкой цитат, в отчёт не попадают — только их число.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.db.enums import (
    DOC_TYPE_TITLES,
    STAGE_TITLES,
    DocTypeSource,
    DocumentStatus,
    FindingStatus,
    RuleOutcome,
    RuleStatus,
    Severity,
    Stage,
)
from svetofor.db.models import (
    Check,
    CheckStage,
    Document,
    Feedback,
    Finding,
    RuleResult,
    RulesetVersion,
)
from svetofor.pipeline.doctype import CERTAIN
from svetofor.pipeline.orchestrator import check_rules, load_context
from svetofor.reports.clarification import deadline_note, request_deadline
from svetofor.rules.aggregate import light_from_severities
from svetofor.rules.facts import merge_card
from svetofor.rules.facts_catalog import FACTS, display_value
from svetofor.rules.model import CATEGORY_TITLES, Rule, RuleCategory
from svetofor.rules.risk_index import RECOMMENDATION_TEXT, RiskModel, category_scores
from svetofor.schemas.checks import (
    CardItem,
    CheckBrief,
    CheckOut,
    DocumentOut,
    EvidenceOut,
    FindingOut,
    LegalRefOut,
    PackageOut,
    ReportOut,
    RiskOut,
    RuleResultOut,
    RulesSummary,
    StageOut,
)

_UNITS = {"working": "раб. дн.", "calendar": "календ. дн."}
_SEVERITY_ORDER = {Severity.RED: 0, Severity.YELLOW: 1}


def ruleset_label(db: Session, check: Check) -> str | None:
    if check.ruleset_version_id is None:
        return None
    version = db.get(RulesetVersion, check.ruleset_version_id)
    return f"{version.ruleset.code} v{version.version}" if version else None


def check_brief(db: Session, check: Check) -> CheckBrief:
    duration = (
        (check.finished_at - check.started_at).total_seconds()
        if check.finished_at and check.started_at
        else None
    )
    return CheckBrief(
        id=check.id,
        status=check.status,
        light=check.light,
        risk_index=check.risk_index,
        law=check.law_detected,
        ruleset=ruleset_label(db, check),
        model=check.llm_model_main,
        created_at=check.created_at,
        started_at=check.started_at,
        finished_at=check.finished_at,
        duration_s=round(duration, 1) if duration is not None else None,
        error=check.error,
    )


def package_out(check: Check) -> PackageOut:
    package = check.package
    return PackageOut(
        id=package.id,
        title=package.title,
        source_filename=package.source_filename,
        is_demo=package.is_demo,
    )


def check_out(db: Session, check: Check) -> CheckOut:
    stages = db.scalars(
        select(CheckStage).where(CheckStage.check_id == check.id).order_by(CheckStage.order_index)
    )
    return CheckOut(
        **check_brief(db, check).model_dump(),
        package=package_out(check),
        stages=[
            StageOut(
                stage=s.stage,
                title=STAGE_TITLES[Stage(s.stage)],
                status=s.status,
                progress=s.progress,
                detail=s.detail,
                started_at=s.started_at,
                finished_at=s.finished_at,
            )
            for s in stages
        ],
        counters=check.counters,
        pipeline=check.pipeline,
    )


def _card(db: Session, check: Check, filenames: dict[str, str]) -> list[CardItem]:
    card = merge_card(load_context(db, check).facts)
    items = []
    for key, spec in FACTS.items():
        fact = card.get(key)
        if fact is None:
            continue
        unit = _UNITS.get(fact.unit or "") if key.endswith("_days") else (fact.unit or spec.unit)
        shown = display_value(key, fact.value)
        items.append(
            CardItem(
                key=key,
                label=spec.label,
                value=fact.value,
                display=f"{shown} {unit}" if unit else shown,
                raw=fact.raw,
                quote=fact.quote,
                document_id=uuid.UUID(fact.document_id),
                filename=filenames.get(fact.document_id, ""),
                page=fact.page_from,
                char_start=fact.char_start,
                char_end=fact.char_end,
                source=fact.source,
                conflict=_conflict_out(key, fact.conflict),
            )
        )
    return items


def _conflict_out(key: str, conflict: dict[str, Any] | None) -> dict[str, Any] | None:
    if not conflict:
        return None
    return {**conflict, "display": display_value(key, conflict.get("value"))}


def _evidence_items(items: list[dict[str, Any]]) -> list[EvidenceOut]:
    return [
        EvidenceOut(
            document_id=uuid.UUID(item["document_id"]),
            quote=item["quote"],
            section_path=item["section_path"],
            page_from=item["page_from"],
            page_to=item["page_to"],
        )
        for item in items
    ]


def _evidence(finding: Finding) -> list[EvidenceOut]:
    return _evidence_items((finding.related or {}).get("evidence", []))


def _fact_value_text(related: dict[str, Any] | None) -> str | None:
    """Значение условия у пороговых правил («30») — для подстановки {value} в вопросах."""
    if not related or "fact" not in related or related.get("value") is None:
        return None
    return display_value(related["fact"], related["value"])


def _finding_out(finding: Finding, rule: Rule | None, feedback: dict[uuid.UUID, Any]) -> FindingOut:
    category = finding.category
    try:
        category_title = CATEGORY_TITLES[RuleCategory(category)]
    except ValueError:
        category_title = category
    return FindingOut(
        id=finding.id,
        rule_code=finding.rule_code,
        severity=finding.severity,
        category=category,
        category_title=category_title,
        kind=finding.kind,
        title=finding.title,
        message=finding.message,
        explanation=finding.explanation,
        quote=finding.quote,
        quote_score=finding.quote_score,
        section_path=finding.section_path,
        page_from=finding.page_from,
        page_to=finding.page_to,
        document_id=finding.document_id,
        highlights=finding.highlights,
        evidence=_evidence(finding),
        checked_documents=(finding.related or {}).get("checked_documents", []),
        search_scope=(finding.related or {}).get("search_scope"),
        value=_fact_value_text(finding.related),
        legal_basis=[
            LegalRefOut(**ref.model_dump(mode="json")) for ref in (rule.legal_basis if rule else [])
        ],
        why_risk=rule.why_risk if rule else "",
        recommendation=rule.recommendation if rule else "",
        rule_status=rule.status if rule else RuleStatus.DRAFT,
        feedback=feedback.get(finding.id),
        review=(finding.related or {}).get("review"),
        needs_review=bool((finding.related or {}).get("needs_review")),
    )


def _documents(documents: list[Document], shown: list[Finding]) -> list[DocumentOut]:
    result = []
    for d in documents:
        own = [f for f in shown if f.document_id == d.id]
        ready = d.status is DocumentStatus.READY
        result.append(
            DocumentOut(
                id=d.id,
                filename=d.filename,
                relative_path=d.relative_path,
                doc_type=d.doc_type,
                doc_type_title=DOC_TYPE_TITLES[d.doc_type],
                doc_type_source=d.doc_type_source,
                doc_type_certain=(
                    d.doc_type_source is DocTypeSource.USER
                    or (d.doc_type_confidence or 0) >= CERTAIN
                ),
                status=d.status,
                problem=None if ready else d.error,
                warning=d.error if ready else None,
                light=light_from_severities(f.severity for f in own) if ready else None,
                pages=d.page_count,
                ocr_pages=d.ocr_pages,
                findings=len(own),
            )
        )
    return result


def _risk(check: Check, shown: list[Finding]) -> RiskOut:
    model = RiskModel.uniform([c.value for c in RuleCategory])
    scores = category_scores([(f.category, f.severity) for f in shown])
    index = check.risk_index
    critical = sum(1 for f in shown if f.severity is Severity.RED)
    recommendation = model.recommend(index, critical) if index is not None else None
    return RiskOut(
        index=index,
        recommendation=recommendation.value if recommendation else None,
        recommendation_text=RECOMMENDATION_TEXT[recommendation] if recommendation else None,
        contributions={k: v for k, v in model.contributions(scores).items() if v},
        weights_source=model.source,
    )


def build_report(db: Session, check: Check, user_id: uuid.UUID | None = None) -> ReportOut:
    rules = {rule.id: rule for _, rule in check_rules(db, check)}
    order = {code: i for i, code in enumerate(rules)}
    documents = list(
        db.scalars(
            select(Document)
            .where(Document.package_id == check.package_id)
            .order_by(Document.relative_path)
        )
    )
    findings = list(db.scalars(select(Finding).where(Finding.check_id == check.id)))
    shown = [f for f in findings if f.status is FindingStatus.SHOWN]
    doc_order = {d.id: i for i, d in enumerate(documents)}
    shown.sort(
        key=lambda f: (
            _SEVERITY_ORDER[f.severity],
            doc_order.get(f.document_id, -1) if f.document_id else -1,
            f.page_from or 0,
            order.get(f.rule_code, 999),
        )
    )
    feedback = {}
    if user_id is not None:
        rows = db.scalars(
            select(Feedback).where(Feedback.check_id == check.id, Feedback.user_id == user_id)
        )
        feedback = {f.finding_id: f.verdict for f in rows if f.finding_id}
    results = list(db.scalars(select(RuleResult).where(RuleResult.check_id == check.id)))
    # правила другого закона к закупке не относятся: ни «проверено», ни «не проверено»
    other_law = sum(1 for r in results if (r.details or {}).get("other_law"))
    results = [r for r in results if not (r.details or {}).get("other_law")]
    results.sort(key=lambda r: order.get(r.rule_code, 999))
    outcomes = Counter(r.outcome for r in results)
    filenames = {str(d.id): d.filename for d in documents}
    report = ReportOut(
        check=check_brief(db, check),
        package=package_out(check),
        card=_card(db, check, filenames),
        documents=_documents(documents, shown),
        findings=[_finding_out(f, rules.get(f.rule_code), feedback) for f in shown],
        rules=[
            RuleResultOut(
                rule_code=r.rule_code,
                title=rules[r.rule_code].title if r.rule_code in rules else r.rule_code,
                category=rules[r.rule_code].category.value if r.rule_code in rules else "",
                type=rules[r.rule_code].type if r.rule_code in rules else "",
                outcome=r.outcome,
                reason=r.reason,
                evidence=_evidence_items((r.details or {}).get("evidence", [])),
                basis=(r.details or {}).get("basis") if r.outcome is RuleOutcome.OK else None,
                blocked=(r.details or {}).get("blocked"),
            )
            for r in results
        ],
        rules_summary=RulesSummary(
            checked=len(results),
            violations=outcomes[RuleOutcome.VIOLATION],
            ok=outcomes[RuleOutcome.OK],
            absent=outcomes[RuleOutcome.ABSENT],
            not_checked=outcomes[RuleOutcome.NOT_CHECKED],
            failed=sum(1 for r in results if (r.details or {}).get("error")),
            unconfirmed=sum(
                1
                for r in results
                if r.outcome is RuleOutcome.OK and (r.details or {}).get("basis") == "unconfirmed"
            ),
            other_law=other_law,
        ),
        risk=_risk(check, shown),
        counters=check.counters,
    )
    report.clarification_deadline = request_deadline(report)
    report.clarification_note = deadline_note(report)
    return report
