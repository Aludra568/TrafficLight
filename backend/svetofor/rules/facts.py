"""Карточка закупки: факты из документов.

Один вызов модели на документ. Каждый факт проходит проверки, прежде чем попасть в карточку:
тип значения соответствует каталогу; цитата реально есть в документе (`verify.py`);
число из значения стоит в цитате («30 дней» не превратится в 10); длинный номер совпадает
цифра в цифру. Не прошедший проверку факт в карточку не попадает — причина сохраняется.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from svetofor.db.enums import DocType
from svetofor.llm.errors import LLMContextTooLongError, LLMError, LLMTruncatedError
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import PromptChunk, alias_chunks, render, resolve_alias
from svetofor.llm.schemas import ExtractedFact, FactsResult
from svetofor.rules.facts_catalog import FACTS, FactKind, FactSpec
from svetofor.rules.numerals import spelled_numbers
from svetofor.rules.verify import QuoteVerifier, TextSource, normalize

FACTS_TASK = "facts"
FACTS_TEMPLATE = "facts.v2"
CONTEXT_TOKENS = 12_000  # больше — фрагменты подбирает поиск по подсказкам фактов
_NUMBER = re.compile(r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_UNITS = {
    "working": "working",
    "рабочих": "working",
    "рабочие": "working",
    "calendar": "calendar",
    "календарных": "calendar",
    "календарные": "calendar",
}
_YES = {"true", "да", "yes"}
_NO = {"false", "нет", "no"}


@dataclass(frozen=True)
class SourceChunk:
    id: str
    document_id: str
    section_path: str
    page_from: int
    page_to: int
    text: str
    char_start: int  # где фрагмент начинается в тексте документа
    token_count: int = 0


@dataclass(frozen=True)
class FactValue:
    key: str
    value: float | bool | str
    unit: str | None
    raw: str | None
    quote: str  # точный текст документа, а не текст модели
    chunk_id: str
    document_id: str
    doc_type: DocType
    page_from: int
    char_start: int  # смещения цитаты в тексте документа
    char_end: int
    score: float  # совпадение цитаты, 0–100
    source: str = "llm"  # llm — модель, rules — грамматика, both — оба канала согласны
    # другой канал нашёл другое значение: {value, unit, quote, char_start, char_end, page}
    conflict: dict[str, Any] | None = None
    # другие значения того же поля в документе (этапы оплаты, расхождение цифр и прописи):
    # [{value, unit, quote, chunk_id, char_start, char_end, page, note?}] — порог берёт худшее
    candidates: tuple[dict[str, Any], ...] = ()

    @property
    def confidence(self) -> float:
        return round(self.score / 100, 3)


@dataclass(frozen=True)
class RejectedFact:
    key: str
    reason: str


@dataclass(frozen=True)
class DocumentFacts:
    document_id: str
    doc_type: DocType
    facts: dict[str, FactValue] = field(default_factory=dict)
    rejected: list[RejectedFact] = field(default_factory=list)
    error: str | None = None  # модель недоступна — карточка по документу «не проверена»
    model: str | None = None
    prompt_version: str | None = None


def facts_for(doc_type: DocType) -> list[FactSpec]:
    return [spec for spec in FACTS.values() if doc_type in spec.sources]


def numbers_in(text: str) -> list[float]:
    """Числа из текста: «1 250 000,00» → 1250000.0, «0,5» → 0.5."""
    cleaned = (n.replace(" ", "").replace(" ", "").replace(",", ".") for n in _NUMBER.findall(text))
    return [float(n) for n in cleaned]


def coerce_value(spec: FactSpec, value: object) -> float | bool | str | None:
    """Значение от модели → тип из каталога. None — значение не того типа."""
    if spec.kind is FactKind.NUMBER:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value) if value >= 0 else None
        found = numbers_in(str(value))
        return found[0] if len(found) == 1 else None
    if spec.kind is FactKind.BOOL:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return True if text in _YES else False if text in _NO else None
    if spec.kind is FactKind.ENUM:
        text = str(value).strip().lower()
        for key, title in spec.choices.items():
            if text in (key.lower(), title.lower()):
                return key
        return None
    text = " ".join(str(value).split())
    return text[:500] if text else None


def value_matches_quote(spec: FactSpec, value: float | bool | str, quote: str) -> bool:
    """Число модели обязано стоять в цитате — цифрами или прописью.

    Раньше цитата без цифр пропускала любое число: «в сроки по графику» → «5 дней».
    """
    if spec.kind is FactKind.NUMBER and isinstance(value, float):
        if value == 0:
            return True  # «аванс не предусмотрен» — числа в цитате нет
        found = numbers_in(quote) or [float(n) for n in spelled_numbers(normalize(quote))]
        return any(abs(n - value) <= 1e-6 * max(1.0, abs(value)) for n in found)
    if spec.kind is FactKind.TEXT and isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 5 and digits == re.sub(r"[\s ]", "", value):
            return digits in re.sub(r"\D", "", quote)  # номер закупки — цифра в цифру
    return True


def _unit(spec: FactSpec, unit: str | None) -> str | None:
    if spec.key.endswith("_days"):
        normalized = _UNITS.get((unit or "").strip().lower())
        if normalized is None and spec.key.endswith("_working_days"):
            return "working"
        return normalized
    if spec.key == "quantity_total" and unit:
        return unit.strip()[:32]
    return None


class FactExtractor:
    def __init__(self, gateway: LLMGateway, verifier: QuoteVerifier) -> None:
        self._gateway = gateway
        self._verifier = verifier

    def extract(
        self,
        *,
        document_id: str,
        doc_type: DocType,
        doc_label: str,
        chunks: Sequence[SourceChunk],
        check_id: uuid.UUID | None = None,
    ) -> DocumentFacts:
        specs = facts_for(doc_type)
        if not specs or not chunks:
            return DocumentFacts(document_id, doc_type)
        return self._extract(specs, document_id, doc_type, doc_label, chunks, check_id)

    def _extract(
        self,
        specs: Sequence[FactSpec],
        document_id: str,
        doc_type: DocType,
        doc_label: str,
        chunks: Sequence[SourceChunk],
        check_id: uuid.UUID | None,
    ) -> DocumentFacts:
        prompt_chunks = alias_chunks(
            [(c.id, c.section_path, c.page_from, c.page_to, c.text) for c in chunks]
        )
        prompt = render(FACTS_TEMPLATE, doc_label=doc_label, facts=specs, chunks=prompt_chunks)
        try:
            result = self._gateway.complete_json(
                task=FACTS_TASK,
                model_role="main",
                system=prompt.system,
                user=prompt.user,
                schema=FactsResult,
                prompt_version=prompt.version,
                max_tokens=3000,
                check_id=check_id,
            )
        except LLMContextTooLongError as exc:
            budget = sum(c.token_count for c in chunks) // 2
            fewer = select_fact_chunks(chunks, specs, budget)
            if not fewer or len(fewer) >= len(chunks):
                return DocumentFacts(document_id, doc_type, error=exc.message)
            return self._extract(specs, document_id, doc_type, doc_label, fewer, check_id)
        except LLMTruncatedError as exc:
            if len(specs) == 1:
                return DocumentFacts(document_id, doc_type, error=exc.message)
            # ответ не уместился в лимит — поля делятся пополам
            middle = len(specs) // 2
            return _combine(
                self._extract(specs[:middle], document_id, doc_type, doc_label, chunks, check_id),
                self._extract(specs[middle:], document_id, doc_type, doc_label, chunks, check_id),
            )
        except LLMError as exc:
            return DocumentFacts(document_id, doc_type, error=exc.message)

        wanted = {spec.key for spec in specs}
        by_id = {c.id: c for c in chunks}
        sources = [TextSource(c.id, c.text) for c in chunks]
        facts: dict[str, FactValue] = {}
        rejected: list[RejectedFact] = []
        for item in result.value.facts:
            if item.key not in wanted or item.key in facts or item.value is None:
                continue
            outcome = self._check(item, doc_type, document_id, by_id, sources, prompt_chunks)
            if isinstance(outcome, RejectedFact):
                rejected.append(outcome)
            else:
                facts[item.key] = outcome
        return DocumentFacts(
            document_id, doc_type, facts, rejected, None, result.model, result.prompt_version
        )

    def _check(
        self,
        item: ExtractedFact,
        doc_type: DocType,
        document_id: str,
        by_id: dict[str, SourceChunk],
        sources: list[TextSource],
        prompt_chunks: Sequence[PromptChunk],
    ) -> FactValue | RejectedFact:
        spec = FACTS[item.key]
        value = coerce_value(spec, item.value)
        if value is None:
            return RejectedFact(item.key, "значение не того типа")
        preferred = resolve_alias(item.chunk_id, prompt_chunks)
        match = self._verifier.verify(
            item.quote, sources, preferred.source_id if preferred else None
        )
        if match is None:
            return RejectedFact(item.key, "цитата не найдена в документе")
        if not value_matches_quote(spec, value, match.text):
            return RejectedFact(item.key, "значение не совпадает с цитатой")
        chunk = by_id[match.source_id]
        return FactValue(
            key=item.key,
            value=value,
            unit=_unit(spec, item.unit),
            raw=item.raw,
            quote=match.text,
            chunk_id=chunk.id,
            document_id=document_id,
            doc_type=doc_type,
            page_from=chunk.page_from,
            char_start=chunk.char_start + match.start,
            char_end=chunk.char_start + match.end,
            score=match.score,
        )


def _combine(first: DocumentFacts, second: DocumentFacts) -> DocumentFacts:
    """Две половины запроса фактов одного документа — в один результат."""
    return DocumentFacts(
        first.document_id,
        first.doc_type,
        {**first.facts, **second.facts},
        [*first.rejected, *second.rejected],
        first.error or second.error,
        first.model or second.model,
        first.prompt_version or second.prompt_version,
    )


def merge_card(documents: Sequence[DocumentFacts]) -> dict[str, FactValue]:
    """Карточка закупки: каждый факт — из самого приоритетного типа документа.

    Приоритет — порядок `FactSpec.sources` (например, НМЦК — из извещения, пеня — из контракта).
    Расхождения между документами ищут правила `cross_document`, а не карточка.
    """
    card: dict[str, FactValue] = {}
    for key, spec in FACTS.items():
        candidates = [d.facts[key] for d in documents if key in d.facts]
        if not candidates:
            continue
        priority = {doc_type: i for i, doc_type in enumerate(spec.sources)}
        card[key] = min(candidates, key=lambda f: (priority.get(f.doc_type, 99), -f.score))
    return card


def procurement_law(documents: Sequence[DocumentFacts]) -> str | None:
    """Закон закупки для применимости правил; None — не определён, применяются правила всех законов.

    Закон решает, какие правила вообще проверять, поэтому одного голоса мало: если грамматика и
    модель или разные документы называют разные законы (или в тексте упомянуты оба), закон не
    определён. Иначе модель, прочитавшая «223-ФЗ» в оговорке «положения 223-ФЗ не применяются»,
    выводила бы пакет из-под правил 44-ФЗ (безопасность зелёного, шаг 1).
    """
    values: set[str] = set()
    for document in documents:
        fact = document.facts.get("law")
        if fact is None:
            continue
        values.add(str(fact.value))
        if fact.conflict and fact.conflict.get("value") is not None:
            values.add(str(fact.conflict["value"]))
        values.update(str(c["value"]) for c in fact.candidates if c.get("value") is not None)
    return values.pop() if len(values) == 1 else None


def fits_whole(chunks: Sequence[SourceChunk], budget: int = CONTEXT_TOKENS) -> bool:
    """Документ целиком помещается в один запрос фактов."""
    return sum(c.token_count for c in chunks) <= budget


def _stems(text: str) -> set[str]:
    """Грубые основы слов (первые 5 букв) — чтобы «оплата» находила «оплаты», «оплату»."""
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", text.lower().replace("ё", "е"))}


def select_fact_chunks(
    chunks: Sequence[SourceChunk], specs: Sequence[FactSpec], budget: int = CONTEXT_TOKENS
) -> list[SourceChunk]:
    """Фрагменты большого документа для запроса фактов: где больше слов из подписей и подсказок."""
    return select_chunks(chunks, " ".join(f"{s.label} {s.hint}" for s in specs), budget)


def select_chunks(
    chunks: Sequence[SourceChunk], words: str, budget: int = CONTEXT_TOKENS
) -> list[SourceChunk]:
    """Фрагменты, где больше слов из `words`, в пределах бюджета токенов.

    Отбор детерминированный (без модели и без поиска), порядок фрагментов — как в документе.
    """
    wanted = _stems(words)
    scored = sorted(
        range(len(chunks)),
        key=lambda i: (-len(wanted & _stems(chunks[i].text)), i),
    )
    chosen: set[int] = set()
    used = 0
    for index in scored:
        cost = chunks[index].token_count
        if used + cost <= budget:
            chosen.add(index)
            used += cost
    return [chunks[i] for i in sorted(chosen)]
