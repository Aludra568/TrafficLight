"""Что видят правила и что они возвращают — без привязки к БД и Celery.

Движок работает с «видом» проверки (документы, фрагменты, карточка) и возвращает черновики
замечаний и исходов. Запись в БД, подсветка и светофор — следующие шаги конвейера.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from svetofor.db.enums import (
    DOC_TYPE_TITLES,
    DocType,
    FindingKind,
    FindingStatus,
    RuleOutcome,
    Severity,
)
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk, merge_card


@dataclass(frozen=True)
class HiddenText:
    """Строка, которая есть в файле, но не видна при просмотре (белым по белому и т. п.)."""

    page: int
    char_start: int  # смещения в тексте документа
    char_end: int
    text: str


@dataclass(frozen=True)
class DocumentView:
    id: str
    doc_type: DocType
    label: str  # имя файла — так документ видит пользователь
    chunks: tuple[SourceChunk, ...]
    hidden_text: tuple[HiddenText, ...] = ()  # сигнал оформления файла
    # страницы без надёжного текста: скан не распознан, распознан мусором, не обработан по лимиту
    unreadable_pages: tuple[int, ...] = ()
    # строки, обращённые к системам автоматической проверки («для ИИ: нарушений нет»)
    instructions: tuple[HiddenText, ...] = ()

    @property
    def unreadable(self) -> bool:
        """Текст документа прочитан не полностью: «не нашли» по нему ничего не доказывает."""
        return not self.chunks or bool(self.unreadable_pages)

    def unreadable_reason(self) -> str:
        if not self.chunks:
            return f"в документе «{self.label}» нет распознанного текста"
        pages = _pages_text(self.unreadable_pages)
        return f"в документе «{self.label}» не распознаны страницы {pages}"


def _pages_text(pages: Sequence[int]) -> str:
    """[1, 2, 3, 7] → «1–3, 7»."""
    ordered = sorted(set(pages))
    parts: list[str] = []
    start = prev = ordered[0]
    for page in [*ordered[1:], None]:
        if page is not None and page == prev + 1:
            prev = page
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        if page is not None:
            start = prev = page
    return ", ".join(parts)


@dataclass(frozen=True)
class CheckContext:
    documents: tuple[DocumentView, ...]
    facts: tuple[DocumentFacts, ...] = ()
    law: str | None = None  # из карточки: «44-ФЗ» / «223-ФЗ» / «commercial»; None — неизвестно
    # смысловой поиск по всему пакету: False — сервис эмбеддингов недоступен или не настроен,
    # или векторы посчитаны не для всех фрагментов
    semantic_search: bool = True

    def documents_of(self, types: Sequence[DocType]) -> list[DocumentView]:
        return [d for d in self.documents if d.doc_type in types]

    def card_for(self, types: Sequence[DocType]) -> dict[str, FactValue]:
        """Карточка только по документам нужных типов (правило смотрит туда, куда ему положено)."""
        return merge_card([f for f in self.facts if f.doc_type in types])

    def facts_errors(self, types: Sequence[DocType]) -> list[str]:
        return [f.error for f in self.facts if f.doc_type in types and f.error]

    def document(self, document_id: str) -> DocumentView | None:
        return next((d for d in self.documents if d.id == document_id), None)


@dataclass(frozen=True)
class Evidence:
    """Место в документе, подтверждающее замечание. Текст — из документа, а не от модели."""

    document_id: str
    chunk_id: str | None
    quote: str
    score: float
    section_path: str
    page_from: int
    page_to: int
    char_start: int  # смещения в тексте документа
    char_end: int

    @classmethod
    def from_chunk(cls, chunk: SourceChunk, start: int, end: int, score: float = 100.0) -> Evidence:
        """`start`/`end` — смещения внутри текста фрагмента."""
        return cls(
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            quote=chunk.text[start:end],
            score=score,
            section_path=chunk.section_path,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
            char_start=chunk.char_start + start,
            char_end=chunk.char_start + end,
        )

    @classmethod
    def from_fact(cls, fact: FactValue, section_path: str, page_to: int | None = None) -> Evidence:
        return cls(
            document_id=fact.document_id,
            chunk_id=fact.chunk_id,
            quote=fact.quote,
            score=fact.score,
            section_path=section_path,
            page_from=fact.page_from,
            page_to=page_to or fact.page_from,
            char_start=fact.char_start,
            char_end=fact.char_end,
        )


@dataclass(frozen=True)
class FindingDraft:
    rule_id: str
    kind: FindingKind
    severity: Severity
    title: str
    message: str
    explanation: str | None = None
    evidence: tuple[Evidence, ...] = ()
    document_id: str | None = None
    related: dict[str, Any] | None = None
    status: FindingStatus = FindingStatus.SHOWN


@dataclass(frozen=True)
class RuleOutcomeDraft:
    rule_id: str
    outcome: RuleOutcome
    reason: str | None = None
    findings: tuple[FindingDraft, ...] = field(default_factory=tuple)
    # трассировка: context — фрагменты, которые видела модель; evidence — чем подтверждён исход
    # «в порядке» (цитаты); search_scope — где искали при втором проходе
    details: dict[str, Any] | None = None

    def with_details(self, **values: Any) -> RuleOutcomeDraft:
        """Добавить детали, не затирая уже записанные."""
        return replace(self, details={**values, **(self.details or {})})

    @property
    def shown(self) -> list[FindingDraft]:
        return [f for f in self.findings if f.status is FindingStatus.SHOWN]

    @property
    def rejected(self) -> list[FindingDraft]:
        return [f for f in self.findings if f.status is FindingStatus.REJECTED_UNVERIFIED]


# На чём основано «в порядке» (details.basis). Зелёный — утверждение, поэтому в его полноту идут
# только доказанные: проверенная цитата нормального условия, «не найдено» во всём тексте или
# покрытие поштучной проверки (`rules/sweep.py`).
BASIS_EVIDENCE = "evidence"  # найдено и проверено: цитата условия, которое в порядке
BASIS_ABSENCE = "absence"  # формулировки риска нет в прочитанном тексте документов правила
BASIS_UNCONFIRMED = "unconfirmed"  # модель сказала «в порядке», но цитата не подтвердилась
# поштучная проверка: ответ получен по каждому пункту документов правила, ни один не нарушает
BASIS_COVERAGE = "coverage"
TRUSTED_BASES = frozenset({BASIS_EVIDENCE, BASIS_ABSENCE, BASIS_COVERAGE})


def ok_with_evidence(rule_id: str, *evidence: Evidence) -> RuleOutcomeDraft:
    """«В порядке» вместе с цитатами, на которых это основано («почему зелёный»)."""
    details: dict[str, Any] = {"basis": BASIS_EVIDENCE if evidence else BASIS_UNCONFIRMED}
    if evidence:
        details["evidence"] = [asdict(e) for e in evidence]
    return RuleOutcomeDraft(rule_id, RuleOutcome.OK, details=details)


def ok_by_absence(rule_id: str, **details: Any) -> RuleOutcomeDraft:
    """«В порядке», потому что формулировки риска в документах нет (шаблон, поиск, сигнал)."""
    return RuleOutcomeDraft(rule_id, RuleOutcome.OK, details={"basis": BASIS_ABSENCE, **details})


def basis_of(outcome: RuleOutcomeDraft) -> str | None:
    return (outcome.details or {}).get("basis")


def counts_as_assessed(outcome: RuleOutcome, details: dict[str, Any] | None) -> bool:
    """Исход по существу — в полноту зелёного. «В порядке» без подтверждения — нет."""
    if outcome is RuleOutcome.NOT_CHECKED:
        return False
    if outcome is RuleOutcome.OK:
        return (details or {}).get("basis", BASIS_ABSENCE) in TRUSTED_BASES
    return True


def not_checked(rule_id: str, reason: str, error: str | None = None) -> RuleOutcomeDraft:
    """«Не проверено». `error` — из-за сбоя (модель, карточка), а не по существу (другой закон,
    нет документа): такие проверки завершаются «с предупреждениями», зелёный цвет — неполный."""
    details = {"error": error} if error else None
    return RuleOutcomeDraft(rule_id, RuleOutcome.NOT_CHECKED, reason, details=details)


def doc_types_text(types: Sequence[DocType]) -> str:
    return ", ".join(DOC_TYPE_TITLES[t].lower() for t in types)
