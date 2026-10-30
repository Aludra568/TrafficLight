"""Аналитическая часть проверки.

Эмбеддинги фрагментов, карточка закупки по каждому документу, исполнение правил с записью
замечаний. Каждая функция — законченный идемпотентный шаг: задачи Celery вызывают их по графу
из документации, тесты — напрямую. Повторный шаг заменяет свои прежние результаты.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from svetofor.core.logging import get_logger
from svetofor.db.enums import DocType, DocumentStatus, FindingStatus
from svetofor.db.models import Check, Chunk, Document, Fact, Finding, RuleResult, RulesetVersion
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import Budgets
from svetofor.rules.engine import RuleEngine
from svetofor.rules.fact_rules import extract_rule_facts, merge_channels
from svetofor.rules.facts import (
    DocumentFacts,
    FactExtractor,
    FactValue,
    SourceChunk,
    facts_for,
    fits_whole,
    procurement_law,
    select_fact_chunks,
)
from svetofor.rules.facts_catalog import FACTS, FactKind
from svetofor.rules.model import LlmCheckRule, Rule
from svetofor.rules.results import (
    CheckContext,
    DocumentView,
    FindingDraft,
    HiddenText,
    RuleOutcomeDraft,
)
from svetofor.rules.signals import find_instructions
from svetofor.rules.store import ruleset_rules
from svetofor.rules.types import EngineDeps, RetrievedChunks, Retriever, SearchRequest
from svetofor.rules.types.llm_check import batches
from svetofor.rules.verify import QuoteVerifier
from svetofor.search.embeddings import Embedder, EmbeddingError
from svetofor.search.hybrid import HybridSearch, Query

log = get_logger(__name__)
SessionFactory = Callable[[], Session]
# Мелкие пачки фрагментов близкой длины: TEI на CPU дополняет пачку до самого длинного текста,
# и пустые токены считаются наравне с настоящими. Замер на пакете ЕИС (119 фрагментов):
# 64 подряд — 81 с, 64 по длине — 60 с, 8 по длине — 48 с; векторы те же
EMBED_BATCH = 8
PROMPT_BUDGET_TOKENS = 6000


def embedding_input(chunk: Chunk) -> str:
    """Текст фрагмента для эмбеддинга: путь раздела + текст (тема бывает только в заголовке)."""
    return f"{chunk.section_path}: {chunk.text}"


def embedding_batches(chunks: Sequence[Chunk]) -> list[list[Chunk]]:
    """Пачки для сервиса эмбеддингов: по длине текста, не больше `EMBED_BATCH` в пачке."""
    ordered = sorted(chunks, key=lambda c: len(embedding_input(c)))
    return [ordered[i : i + EMBED_BATCH] for i in range(0, len(ordered), EMBED_BATCH)]


@dataclass
class AnalysisDeps:
    verifier: QuoteVerifier
    gateway: LLMGateway | None = None
    embedder: Embedder | None = None
    context_tokens: int = 32768  # длина контекста модели (LLM_CONTEXT_TOKENS)
    second_opinion: bool = True  # перепроверять красные нарушения модели (LLM_SECOND_OPINION)
    green_sweep: bool = False  # поштучная проверка пунктов перед зелёным (GREEN_SWEEP)
    concurrency: int = 4  # параллельных вызовов модели в поштучной проверке (LLM_MAX_CONCURRENCY)

    @property
    def budgets(self) -> Budgets:
        return Budgets.for_context(self.context_tokens)


def source_chunk(chunk: Chunk) -> SourceChunk:
    return SourceChunk(
        id=str(chunk.id),
        document_id=str(chunk.document_id),
        section_path=chunk.section_path,
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        text=chunk.text,
        char_start=chunk.char_start,
        token_count=chunk.token_count,
    )


def _ready_documents(db: Session, package_id: uuid.UUID) -> list[Document]:
    statement = select(Document).where(
        Document.package_id == package_id, Document.status == DocumentStatus.READY
    )
    return list(db.scalars(statement.order_by(Document.relative_path)))


def _chunks(db: Session, document_id: uuid.UUID) -> list[Chunk]:
    statement = select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.order_index)
    return list(db.scalars(statement))


# ─────────────────────────── Эмбеддинги ───────────────────────────


def mark_vector_search(session_factory: SessionFactory, check_id: uuid.UUID, state: str) -> None:
    """Смысловой поиск не работал («unavailable» — сбой сервиса, «disabled» — не настроен).

    Поиск тогда идёт только по словам и хуже находит перефразировки — это видно в отчёте, а не
    только в журнале.
    """
    with session_factory() as db:
        check = db.get(Check, check_id, with_for_update=True)
        if check is not None:
            check.counters = {**check.counters, "vector_search": state}
            db.commit()


def embed_chunks(session_factory: SessionFactory, check_id: uuid.UUID, embedder: Embedder) -> int:
    """Векторы для фрагментов пакета, у которых их ещё нет. Сбой сервиса — поиск по тексту."""
    with session_factory() as db:
        check = db.get(Check, check_id)
        if check is None:
            return 0
        statement = (
            select(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.package_id == check.package_id, Chunk.embedding.is_(None))
            .order_by(Chunk.document_id, Chunk.order_index)
        )
        chunks = list(db.scalars(statement))
        done = 0
        for batch in embedding_batches(chunks):
            try:
                vectors = embedder.embed_passages([embedding_input(c) for c in batch])
            except EmbeddingError:
                log.warning("embeddings_unavailable", check_id=str(check_id))
                db.rollback()
                mark_vector_search(session_factory, check_id, "unavailable")
                break
            for chunk, vector in zip(batch, vectors, strict=True):
                chunk.embedding = vector
            db.commit()
            done += len(batch)
        return done


# ─────────────────────────── Карточка закупки ───────────────────────────


def _fact_row(check_id: uuid.UUID, fact: FactValue) -> Fact:
    kind = FACTS[fact.key].kind
    numeric = kind in (FactKind.NUMBER, FactKind.BOOL)
    return Fact(
        check_id=check_id,
        document_id=uuid.UUID(fact.document_id),
        key=fact.key,
        value_num=float(fact.value) if numeric else None,
        value_text=None if numeric else str(fact.value),
        unit=fact.unit,
        raw=fact.raw,
        quote=fact.quote,
        chunk_id=uuid.UUID(fact.chunk_id),
        page_from=fact.page_from,
        char_start=fact.char_start,
        char_end=fact.char_end,
        confidence=fact.confidence,
        verified=True,
        source=fact.source,
        conflict=fact.conflict,
        candidates=list(fact.candidates) or None,
    )


def _update_fact_counters(db: Session, check_id: uuid.UUID, result: DocumentFacts) -> None:
    """Ошибки и отклонённые факты документа — в счётчики проверки.

    Строка проверки блокируется: задачи по разным документам идут параллельно и иначе
    затирали бы записи друг друга.
    """
    check = db.get(Check, check_id, with_for_update=True)
    if check is None:
        return
    errors = dict(check.counters.get("facts_errors", {}))
    rejected = dict(check.counters.get("facts_rejected", {}))
    errors.pop(result.document_id, None)
    rejected.pop(result.document_id, None)
    if result.error:
        errors[result.document_id] = result.error
    if result.rejected:
        rejected[result.document_id] = [asdict(r) for r in result.rejected]
    check.counters = {**check.counters, "facts_errors": errors, "facts_rejected": rejected}


def extract_document_facts(
    session_factory: SessionFactory, check_id: uuid.UUID, document_id: uuid.UUID, deps: AnalysisDeps
) -> DocumentFacts | None:
    """Факты одного документа (один вызов модели) с проверкой каждой цитаты."""
    with session_factory() as db:
        document = db.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.READY:
            return None
        specs = facts_for(document.doc_type)
        chunks = [source_chunk(c) for c in _chunks(db, document_id)]
        doc_type, label = document.doc_type, Path(document.filename).stem
    if not specs or not chunks:
        return None
    # канал 1 — грамматики: работает всегда, без модели, числа не выдумывает
    by_rules = extract_rule_facts(str(document_id), doc_type, chunks)
    # канал 2 — модель; каналы сверяются, расхождение сохраняется и показывается
    if deps.gateway is None:
        by_model = DocumentFacts(str(document_id), doc_type, error="нет доступа к модели")
    else:
        whole = deps.budgets.whole
        selected = chunks if fits_whole(chunks, whole) else select_fact_chunks(chunks, specs, whole)
        extractor = FactExtractor(deps.gateway, deps.verifier)
        by_model = extractor.extract(
            document_id=str(document_id),
            doc_type=doc_type,
            doc_label=label,
            chunks=selected,
            check_id=check_id,
        )
    result = merge_channels(by_rules, by_model)
    with session_factory() as db:
        # строка проверки — первой: вставка фактов берёт по внешнему ключу разделяемую блокировку
        # проверки, и две задачи, вставившие факты до `FOR UPDATE` счётчиков, ждали бы друг друга
        # (DeadlockDetected в Docker, где документы идут параллельно)
        db.execute(select(Check.id).where(Check.id == check_id).with_for_update())
        db.execute(delete(Fact).where(Fact.check_id == check_id, Fact.document_id == document_id))
        for fact in result.facts.values():
            db.add(_fact_row(check_id, fact))
        _update_fact_counters(db, check_id, result)
        db.commit()
    return result


def _fact_value(row: Fact, doc_type: DocType) -> FactValue | None:
    spec = FACTS.get(row.key)
    if spec is None or row.quote is None or row.chunk_id is None:
        return None
    value: float | bool | str
    if spec.kind is FactKind.BOOL:
        value = bool(row.value_num)
    elif spec.kind is FactKind.NUMBER:
        if row.value_num is None:
            return None
        value = row.value_num
    else:
        value = row.value_text or ""
    return FactValue(
        key=row.key,
        value=value,
        unit=row.unit,
        raw=row.raw,
        quote=row.quote,
        chunk_id=str(row.chunk_id),
        document_id=str(row.document_id),
        doc_type=doc_type,
        page_from=row.page_from or 1,
        char_start=row.char_start or 0,
        char_end=row.char_end or 0,
        score=round((row.confidence or 0) * 100, 1),
        source=row.source,
        conflict=row.conflict,
        candidates=tuple(row.candidates or ()),
    )


# ─────────────────────────── Контекст и правила ───────────────────────────


def load_context(db: Session, check: Check) -> CheckContext:
    documents = _ready_documents(db, check.package_id)
    views = []
    for d in documents:
        chunks = tuple(source_chunk(c) for c in _chunks(db, d.id))
        signals = d.signals or {}
        views.append(
            DocumentView(
                str(d.id),
                d.doc_type,
                d.filename,
                chunks,
                tuple(HiddenText(**item) for item in signals.get("hidden_text", [])),
                tuple(int(p) for p in signals.get("unreadable_pages", [])),
                find_instructions(chunks),
            )
        )
    errors: dict[str, str] = check.counters.get("facts_errors", {})
    rows = list(db.scalars(select(Fact).where(Fact.check_id == check.id)))
    facts: list[DocumentFacts] = []
    for document in documents:
        values = [_fact_value(r, document.doc_type) for r in rows if r.document_id == document.id]
        facts.append(
            DocumentFacts(
                str(document.id),
                document.doc_type,
                {v.key: v for v in values if v is not None},
                error=errors.get(str(document.id)),
            )
        )
    law = procurement_law(facts)
    semantic = check.counters.get("vector_search") not in ("unavailable", "disabled")
    return CheckContext(tuple(views), tuple(facts), law, semantic)


def make_retriever(
    db: Session,
    package_id: uuid.UUID,
    embedder: Embedder | None,
    budget_tokens: int = PROMPT_BUDGET_TOKENS,
) -> Retriever:
    search = HybridSearch(db, embedder)

    def retrieve(
        doc_types: Sequence[DocType],
        requests: Sequence[SearchRequest],
        budget: int | None = None,
    ) -> list[SourceChunk]:
        queries = [Query(r.text, r.hints) for r in requests]
        result = search.search(package_id=package_id, doc_types=doc_types, queries=queries)
        chunks = search.expand(result.hits, budget or budget_tokens)
        found = RetrievedChunks(source_chunk(c) for c in chunks)
        found.semantic = result.vector_used
        return found

    return retrieve


def check_rules(db: Session, check: Check) -> list[tuple[uuid.UUID, Rule]]:
    if check.ruleset_version_id is None:
        return []
    version = db.get(RulesetVersion, check.ruleset_version_id)
    return ruleset_rules(db, version) if version else []


def rule_batches(rules: Sequence[Rule], max_rules_per_call: int = 6) -> list[list[str]]:
    """Как делить правила между задачами: все без модели — одна задача, группы модели — по одной."""
    enabled = [r for r in rules if r.enabled]
    plain = [r.id for r in enabled if not isinstance(r, LlmCheckRule)]
    llm = [r for r in enabled if isinstance(r, LlmCheckRule)]
    result = [plain] if plain else []
    result.extend([r.id for r in batch] for batch in batches(llm, max_rules_per_call))
    return result


def evaluate_rules(
    session_factory: SessionFactory,
    check_id: uuid.UUID,
    deps: AnalysisDeps,
    rule_codes: Sequence[str] | None = None,
) -> list[RuleOutcomeDraft]:
    """Исполнить правила (все или указанные) и записать исходы и замечания."""
    with session_factory() as db:
        check = db.get(Check, check_id)
        if check is None:
            return []
        versioned = check_rules(db, check)
        if rule_codes is not None:
            wanted = set(rule_codes)
            versioned = [(v, r) for v, r in versioned if r.id in wanted]
        ctx = load_context(db, check)
        engine = RuleEngine(
            EngineDeps(
                verifier=deps.verifier,
                gateway=deps.gateway,
                retrieve=make_retriever(
                    db, check.package_id, deps.embedder, deps.budgets.retrieval
                ),
                check_id=check_id,
                whole_budget=deps.budgets.whole,
                second_opinion=deps.second_opinion,
            )
        )
        outcomes = engine.run([r for _, r in versioned], ctx)
        _save_outcomes(db, check_id, versioned, outcomes)
        db.commit()
        return outcomes


def _save_outcomes(
    db: Session,
    check_id: uuid.UUID,
    versioned: Sequence[tuple[uuid.UUID, Rule]],
    outcomes: Sequence[RuleOutcomeDraft],
) -> None:
    codes = [r.id for _, r in versioned]
    db.execute(delete(Finding).where(Finding.check_id == check_id, Finding.rule_code.in_(codes)))
    db.execute(
        delete(RuleResult).where(RuleResult.check_id == check_id, RuleResult.rule_code.in_(codes))
    )
    by_code = {r.id: (version_id, r) for version_id, r in versioned}
    for outcome in outcomes:
        version_id, rule = by_code[outcome.rule_id]
        db.add(
            RuleResult(
                check_id=check_id,
                rule_version_id=version_id,
                rule_code=rule.id,
                outcome=outcome.outcome,
                reason=outcome.reason,
                details=outcome.details,
            )
        )
        for draft in outcome.findings:
            db.add(finding_row(check_id, version_id, rule, draft))


def finding_row(
    check_id: uuid.UUID, version_id: uuid.UUID, rule: Rule, draft: FindingDraft
) -> Finding:
    main = draft.evidence[0] if draft.evidence else None
    related: dict[str, Any] = dict(draft.related or {})
    if draft.evidence:
        related["evidence"] = [asdict(e) for e in draft.evidence]
    return Finding(
        check_id=check_id,
        document_id=uuid.UUID(draft.document_id) if draft.document_id else None,
        rule_version_id=version_id,
        rule_code=rule.id,
        category=rule.category.value,
        severity=draft.severity,
        kind=draft.kind,
        title=draft.title,
        message=draft.message,
        explanation=draft.explanation,
        quote=main.quote if main else None,
        quote_score=main.score if main else None,
        section_path=main.section_path if main else None,
        page_from=main.page_from if main else None,
        page_to=main.page_to if main else None,
        char_start=main.char_start if main else None,
        char_end=main.char_end if main else None,
        highlights=[],
        related=related or None,
        status=draft.status if draft.status else FindingStatus.SHOWN,
    )
