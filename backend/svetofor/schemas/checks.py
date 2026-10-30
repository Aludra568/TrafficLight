"""Схемы API проверок и отчёта."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from svetofor.db.enums import (
    CheckStatus,
    DocType,
    DocTypeSource,
    DocumentStatus,
    FeedbackVerdict,
    FindingKind,
    Light,
    RuleOutcome,
    RuleStatus,
    Severity,
    StageStatus,
)


class CheckCreated(BaseModel):
    package_id: uuid.UUID
    check_id: uuid.UUID


class EisIn(BaseModel):
    """Реестровый номер закупки в ЕИС: 19 цифр (44-ФЗ) или 11 цифр (223-ФЗ)."""

    number: str = Field(max_length=40)


class EisTask(BaseModel):
    task_id: str


class EisStatus(BaseModel):
    status: Literal["pending", "done", "failed"]
    check_id: uuid.UUID | None = None
    package_id: uuid.UUID | None = None
    error: str | None = None


class PackageOut(BaseModel):
    id: uuid.UUID
    title: str
    source_filename: str
    is_demo: bool


class StageOut(BaseModel):
    stage: str
    title: str
    status: StageStatus
    progress: int
    detail: str | None
    started_at: datetime | None
    finished_at: datetime | None


class CheckBrief(BaseModel):
    id: uuid.UUID
    status: CheckStatus
    light: Light | None
    risk_index: float | None
    law: str | None = Field(description="Закон закупки из карточки")
    ruleset: str | None = Field(description="Набор правил и его версия: «44fz-supplier-base v3»")
    model: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_s: float | None
    error: str | None


class CheckOut(CheckBrief):
    package: PackageOut
    stages: list[StageOut]
    counters: dict[str, Any]
    pipeline: dict[str, Any] | None = Field(
        None, description="Версии OCR, LibreOffice, разборщика, модель эмбеддингов, пороги"
    )


class CheckListItem(BaseModel):
    id: uuid.UUID
    package_id: uuid.UUID
    package_title: str
    status: CheckStatus
    light: Light | None
    risk_index: float | None
    findings: int
    created_at: datetime
    finished_at: datetime | None


class CheckPage(BaseModel):
    items: list[CheckListItem]
    total: int
    page: int
    page_size: int


class CardItem(BaseModel):
    key: str
    label: str
    value: float | bool | str
    display: str = Field(description="Значение для показа: «7 раб. дн.», «запрет»")
    raw: str | None
    quote: str
    document_id: uuid.UUID
    filename: str
    page: int
    char_start: int
    char_end: int
    source: str = Field(description="llm — модель, rules — грамматика, both — оба способа согласны")
    conflict: dict[str, Any] | None = Field(
        None, description="Способы разошлись: другое значение, его цитата и страница"
    )


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    relative_path: str
    doc_type: DocType
    doc_type_title: str
    doc_type_source: DocTypeSource
    doc_type_certain: bool = Field(description="False — интерфейс спрашивает: «Похоже на … Верно?»")
    status: DocumentStatus
    problem: str | None = Field(description="Почему не обработан или пропущен")
    warning: str | None = Field(description="Предупреждение у обработанного документа")
    light: Light | None
    pages: int
    ocr_pages: int
    findings: int


class LegalRefOut(BaseModel):
    act: str
    ref: str | None = None
    note: str | None = None
    edition_checked: str | None = None


class EvidenceOut(BaseModel):
    document_id: uuid.UUID
    quote: str
    section_path: str
    page_from: int
    page_to: int


class ReviewOut(BaseModel):
    decision: Literal["confirmed", "rejected", "unsure"]
    comment: str | None = Field(None, description="Довод второго мнения")


class FindingOut(BaseModel):
    id: uuid.UUID
    rule_code: str
    severity: Severity
    category: str
    category_title: str
    kind: FindingKind
    title: str
    message: str
    explanation: str | None
    quote: str | None
    quote_score: float | None
    section_path: str | None
    page_from: int | None
    page_to: int | None
    document_id: uuid.UUID | None
    highlights: list[dict[str, Any]]
    evidence: list[EvidenceOut] = Field(
        description="Все цитаты; у противоречий — из обоих документов"
    )
    checked_documents: list[str] = Field(description="Где искали, если условие не найдено")
    search_scope: str | None = Field(
        None, description="Как искали отсутствующее условие: «весь текст: …» или часть"
    )
    value: str | None = Field(None, description="Значение условия у пороговых правил: «30»")
    legal_basis: list[LegalRefOut]
    why_risk: str
    recommendation: str
    rule_status: RuleStatus
    feedback: FeedbackVerdict | None = Field(description="Отметка текущего пользователя")
    review: ReviewOut | None = Field(None, description="Второе мнение модели (красные нарушения)")
    needs_review: bool = Field(
        False, description="Второе мнение расходится — требует проверки специалистом"
    )


class RuleResultOut(BaseModel):
    rule_code: str
    title: str
    category: str
    type: str
    outcome: RuleOutcome
    reason: str | None
    evidence: list[EvidenceOut] = Field(
        default_factory=list, description="Чем подтверждено «в порядке»: проверенные цитаты"
    )
    basis: str | None = Field(
        None,
        description="На чём основано «в порядке»: evidence — цитата, absence — формулировки нет "
        "в прочитанном тексте, coverage — поштучная проверка: ответ по каждому пункту, нарушений "
        "нет, unconfirmed — модель без подтверждённой цитаты",
    )
    blocked: str | None = Field(
        None,
        description="Почему «не проверено»: unreadable — текст не прочитан, instructions — "
        "в документе указания программе проверки",
    )


class RulesSummary(BaseModel):
    checked: int
    failed: int = Field(0, description="Из «не проверено» — из-за сбоя модели или карточки")
    violations: int
    ok: int
    absent: int
    not_checked: int
    unconfirmed: int = Field(
        0, description="Из «в порядке» — без подтверждённой цитаты (не входят в полноту)"
    )
    other_law: int = Field(
        0,
        description="Правила другого закона (44-ФЗ для закупки по 223-ФЗ и наоборот): к закупке "
        "не относятся, в списке правил и в полноте не участвуют",
    )


class RiskOut(BaseModel):
    index: float | None
    recommendation: str | None
    recommendation_text: str | None
    contributions: dict[str, float]
    weights_source: str


class ReportOut(BaseModel):
    check: CheckBrief
    package: PackageOut
    card: list[CardItem]
    documents: list[DocumentOut]
    findings: list[FindingOut]
    rules: list[RuleResultOut]
    rules_summary: RulesSummary
    risk: RiskOut
    counters: dict[str, Any]
    clarification_deadline: str | None = Field(
        None,
        description="Последний день запроса о разъяснениях (44-ФЗ: за три дня до окончания "
        "подачи заявок, ДД.ММ.ГГГГ); null — дата подачи заявок не найдена",
    )
    clarification_note: str | None = Field(
        None,
        description="Подсказка о сроке и числе запросов на разъяснение (только 44-ФЗ); null — "
        "для 223-ФЗ и коммерческих закупок порядок задаёт положение о закупке",
    )


class DocumentPatch(BaseModel):
    doc_type: DocType


class FeedbackIn(BaseModel):
    verdict: FeedbackVerdict
    comment: str | None = Field(None, max_length=2000)


class MissedIn(BaseModel):
    rule_code: str | None = Field(None, max_length=32)
    comment: str = Field(min_length=3, max_length=2000)


class ClarificationIn(BaseModel):
    finding_ids: list[uuid.UUID] | None = Field(
        None, description="Какие замечания включить; по умолчанию — все показанные"
    )
