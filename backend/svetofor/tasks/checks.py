"""Задачи проверки пакета: распаковка и документы → факты → правила → итог.

run_check → prepare → chord(process_document × N) → after_documents →
chord(embed_chunks, extract_facts × M) → start_rules → chord(evaluate_rules × K) →
[sweep, если GREEN_SWEEP] → finalize

Задачи тонкие: вся логика — в `svetofor.pipeline`. Каждая задача ловит свои неожиданные ошибки
и записывает честный исход («не проверено» / проверка не удалась): упавшая задача не должна
оборвать chord и оставить проверку навсегда «в работе».
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from celery import chord
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.core.config import Settings, get_settings
from svetofor.core.logging import get_logger
from svetofor.db.enums import DocumentStatus, RuleOutcome, Stage
from svetofor.db.models import Check, Document, Fact, RuleResult
from svetofor.db.session import get_sessionmaker
from svetofor.llm.cache import LLMCache
from svetofor.pipeline.finalize import (
    fail_check,
    fail_stalled,
    finalize_check,
    start_check,
)
from svetofor.pipeline.ingest import (
    CheckFailedError,
    IngestConfig,
    classify_with_llm,
    mark_document_failed,
    prepare_package,
    process_document,
    register_attempt,
)
from svetofor.pipeline.orchestrator import (
    AnalysisDeps,
    check_rules,
    embed_chunks,
    evaluate_rules,
    extract_document_facts,
    mark_vector_search,
    rule_batches,
)
from svetofor.pipeline.progress import StageTracker
from svetofor.pipeline.sweep import fail_sweep, sweep_rules
from svetofor.pipeline.unpack import ARCHIVE_TIMEOUT
from svetofor.rules.facts import facts_for
from svetofor.rules.model import LlmCheckRule
from svetofor.rules.verify import QuoteVerifier
from svetofor.tasks.celery_app import QUEUE_DEFAULT, QUEUE_LLM, celery_app

log = get_logger(__name__)
INTERNAL_ERROR = "Внутренняя ошибка сервиса — попробуйте запустить проверку ещё раз"
UNPACK_TOO_LONG = "Распаковка пакета заняла слишком много времени. Разделите пакет на части"
# распаковка RAR/7z ограничена ARCHIVE_TIMEOUT, плюс чтение заголовков и вложенные архивы:
# лимит задачи меньше — и большой архив обрывался «внутренней ошибкой» вместо понятной причины
PREPARE_TIME_LIMIT = ARCHIVE_TIMEOUT + 180


@dataclass
class Services:
    settings: Settings
    session_factory: Callable[[], Session]
    ingest: IngestConfig
    analysis: AnalysisDeps


@lru_cache
def _default_services() -> Services:
    """Подключения к Redis, модели и сервису эмбеддингов — один раз на процесс воркера."""
    return build_services(get_settings())


def build_services(settings: Settings, record_cassette: Path | None = None) -> Services:
    """Сервисы по настройкам. `record_cassette` — дописывать ответы модели в кассету демо."""
    import redis

    from svetofor.llm.cache import DbCache, JsonlCache, LayeredCache, ReadOnlyCache, RedisCache
    from svetofor.llm.gateway import DbCallLog, build_gateway
    from svetofor.llm.ratelimit import RateLimiter, RedisStore
    from svetofor.search.embeddings import Embedder, RedisEmbeddingCache

    factory = get_sessionmaker()
    client = redis.Redis.from_url(settings.redis_url)
    layers: list[LLMCache] = [RedisCache(client), DbCache(factory)]
    if settings.demo_cassette.is_file():
        # ответы модели для демо-пакета: «Попробовать на примере» без ключа
        layers.append(ReadOnlyCache(JsonlCache(settings.demo_cassette)))
    gateway = build_gateway(
        settings,
        cache=LayeredCache(*layers),
        limiter=RateLimiter(RedisStore(client), rpm=settings.llm_rpm, tpm=settings.llm_tpm),
        call_log=DbCallLog(factory),
        cassette=JsonlCache(record_cassette) if record_cassette else None,
    )
    # EMBED_URL пустой — облегчённая установка без сервиса эмбеддингов: поиск только по тексту
    embedder = (
        Embedder(
            url=settings.embed_url,
            model=settings.embed_model,
            dim=settings.embed_dim,
            cache=RedisEmbeddingCache(client),
        )
        if settings.embed_url
        else None
    )
    analysis = AnalysisDeps(
        QuoteVerifier(min_score=settings.quote_min_score),
        gateway,
        embedder,
        settings.llm_context_tokens,
        settings.llm_second_opinion,
        settings.green_sweep,
        settings.llm_max_concurrency,
    )
    return Services(settings, factory, IngestConfig.from_settings(settings), analysis)


_override: Services | None = None


def configure(services: Services | None) -> None:
    """Подменить зависимости (тесты, оценка качества). None — вернуть настройки окружения."""
    global _override
    _override = services


def services() -> Services:
    return _override or _default_services()


def _tracker(check_id: str) -> StageTracker:
    return StageTracker(services().session_factory, uuid.UUID(check_id))


def _sweep_stalled(s: Services) -> None:
    """Каждая новая проверка заодно закрывает зависшие (сторож без отдельного планировщика)."""
    try:
        fail_stalled(s.session_factory)
    except Exception:
        log.warning("stalled_sweep_failed", exc_info=True)


def _run_chord(header: list[Any], body: Any) -> None:
    if header:
        chord(header)(body)
    else:
        body.apply_async()


# ─────────────────────────── Подготовка документов ───────────────────────────


@celery_app.task(
    name="checks.run_check", soft_time_limit=PREPARE_TIME_LIMIT, time_limit=PREPARE_TIME_LIMIT + 60
)
def run_check(check_id: str) -> None:
    s = services()
    cid = uuid.UUID(check_id)
    _sweep_stalled(s)
    if not start_check(s.session_factory, cid):
        log.info("check_not_started", check_id=check_id)
        return
    try:
        document_ids = prepare_package(s.session_factory, cid, s.ingest)
    except CheckFailedError as exc:
        fail_check(s.session_factory, cid, str(exc))
        return
    except SoftTimeLimitExceeded:
        log.warning("prepare_timeout", check_id=check_id)
        _tracker(check_id).fail(Stage.UNPACK, UNPACK_TOO_LONG)
        fail_check(s.session_factory, cid, UNPACK_TOO_LONG)
        return
    except Exception:
        log.exception("prepare_failed", check_id=check_id)
        fail_check(s.session_factory, cid, INTERNAL_ERROR)
        return
    _tracker(check_id).start(Stage.CONVERT, f"Документов: {len(document_ids)}")
    header = [process_document_task.si(check_id, str(d)) for d in document_ids]
    _run_chord(header, after_documents.si(check_id))


@celery_app.task(name="checks.process_document", soft_time_limit=900, time_limit=960)
def process_document_task(check_id: str, document_id: str) -> str:
    s = services()
    did = uuid.UUID(document_id)
    status = DocumentStatus.FAILED
    try:
        if register_attempt(s.session_factory, did):
            gateway = s.analysis.gateway
            classify = classify_with_llm(gateway) if gateway is not None else None
            status = process_document(s.session_factory, did, s.ingest, classify)
    except Exception:  # исключение оборвало бы chord, и проверка осталась бы «в работе»
        log.exception("process_document_task_failed", document_id=document_id)
        mark_document_failed(s.session_factory, did)
    try:
        _report_document_progress(check_id)
    except Exception:
        log.warning("document_progress_failed", check_id=check_id, exc_info=True)
    return status.value


def _report_document_progress(check_id: str) -> None:
    with services().session_factory() as db:
        check = db.get(Check, uuid.UUID(check_id))
        if check is None:
            return
        statuses = list(
            db.scalars(select(Document.status).where(Document.package_id == check.package_id))
        )
    total = sum(1 for s in statuses if s is not DocumentStatus.SKIPPED)
    done = sum(1 for s in statuses if s in (DocumentStatus.READY, DocumentStatus.FAILED))
    if total:
        _tracker(check_id).progress(
            Stage.CONVERT, round(100 * done / total), f"Документ {done} из {total}"
        )


@celery_app.task(name="checks.after_documents", soft_time_limit=60)
def after_documents(check_id: str) -> None:
    s = services()
    tracker = _tracker(check_id)
    try:
        with s.session_factory() as db:
            check = db.get(Check, uuid.UUID(check_id))
            documents = (
                list(db.scalars(select(Document).where(Document.package_id == check.package_id)))
                if check
                else []
            )
        ready = [d for d in documents if d.status is DocumentStatus.READY]
        failed = sum(1 for d in documents if d.status is DocumentStatus.FAILED)
        ocr_pages = sum(d.ocr_pages for d in ready)
        tracker.done(
            Stage.CONVERT,
            f"Готово документов: {len(ready)}" + (f", не удалось: {failed}" if failed else ""),
        )
        if ocr_pages:
            tracker.done(Stage.OCR, f"Распознано страниц: {ocr_pages}")
        else:
            tracker.skip(Stage.OCR, "Сканов нет")
        tracker.done(Stage.STRUCTURE)
        tracker.start(Stage.FACTS)
        with_facts = [d.id for d in ready if facts_for(d.doc_type)]
    except Exception:
        log.exception("after_documents_failed", check_id=check_id)
        fail_check(s.session_factory, uuid.UUID(check_id), INTERNAL_ERROR)
        return
    header = [embed_chunks_task.si(check_id)]
    header += [extract_facts_task.si(check_id, str(d)) for d in with_facts]
    _run_chord(header, start_rules.si(check_id))


# ─────────────────────────── Карточка и правила ───────────────────────────


@celery_app.task(name="checks.embed_chunks", soft_time_limit=600)
def embed_chunks_task(check_id: str) -> None:
    s = services()
    if s.analysis.embedder is None:
        _safe_mark_vector_search(check_id, "disabled")
        return
    try:
        embed_chunks(s.session_factory, uuid.UUID(check_id), s.analysis.embedder)
    except Exception:  # без векторов поиск работает по тексту — проверку не роняем
        log.exception("embed_failed", check_id=check_id)
        _safe_mark_vector_search(check_id, "unavailable")


def _safe_mark_vector_search(check_id: str, state: str) -> None:
    try:
        mark_vector_search(services().session_factory, uuid.UUID(check_id), state)
    except Exception:
        log.warning("vector_search_mark_failed", check_id=check_id, exc_info=True)


@celery_app.task(name="checks.extract_facts", soft_time_limit=300)
def extract_facts_task(check_id: str, document_id: str) -> None:
    s = services()
    try:
        extract_document_facts(
            s.session_factory, uuid.UUID(check_id), uuid.UUID(document_id), s.analysis
        )
    except Exception:
        log.exception("facts_failed", check_id=check_id, document_id=document_id)
        _mark_facts_error(check_id, document_id)


def _mark_facts_error(check_id: str, document_id: str) -> None:
    with services().session_factory() as db:
        check = db.get(Check, uuid.UUID(check_id), with_for_update=True)
        if check is not None:
            errors = {**check.counters.get("facts_errors", {}), document_id: INTERNAL_ERROR}
            check.counters = {**check.counters, "facts_errors": errors}
            db.commit()


@celery_app.task(name="checks.start_rules", soft_time_limit=60)
def start_rules(check_id: str) -> None:
    s = services()
    tracker = _tracker(check_id)
    try:
        with s.session_factory() as db:
            facts = db.scalars(select(Fact.id).where(Fact.check_id == uuid.UUID(check_id))).all()
            check = db.get(Check, uuid.UUID(check_id))
            rules = [r for _, r in check_rules(db, check)] if check else []
        tracker.done(Stage.FACTS, f"Найдено условий: {len(facts)}")
        tracker.start(Stage.RULES, f"Правил: {sum(1 for r in rules if r.enabled)}")
        llm_codes = {r.id for r in rules if isinstance(r, LlmCheckRule)}
        header = []
        for codes in rule_batches(rules):
            queue = QUEUE_LLM if codes and codes[0] in llm_codes else QUEUE_DEFAULT
            header.append(evaluate_rules_task.si(check_id, codes).set(queue=queue))
    except Exception:
        log.exception("start_rules_failed", check_id=check_id)
        fail_check(s.session_factory, uuid.UUID(check_id), INTERNAL_ERROR)
        return
    if s.analysis.green_sweep:  # вызовы модели — в очередь модели, итог — в конце задачи
        _run_chord(header, sweep_task.si(check_id).set(queue=QUEUE_LLM))
    else:
        _run_chord(header, finalize_task.si(check_id))


@celery_app.task(name="checks.evaluate_rules", soft_time_limit=600)
def evaluate_rules_task(check_id: str, rule_codes: list[str]) -> None:
    s = services()
    try:
        evaluate_rules(s.session_factory, uuid.UUID(check_id), s.analysis, rule_codes)
    except Exception:
        log.exception("rules_failed", check_id=check_id, rules=rule_codes)
        _mark_not_checked(check_id, rule_codes)


def _mark_not_checked(check_id: str, rule_codes: list[str]) -> None:
    """Сбой задачи — правила честно «не проверены», а не потеряны."""
    with services().session_factory() as db:
        cid = uuid.UUID(check_id)
        existing = set(
            db.scalars(
                select(RuleResult.rule_code).where(
                    RuleResult.check_id == cid, RuleResult.rule_code.in_(rule_codes)
                )
            )
        )
        for code in rule_codes:
            if code not in existing:
                db.add(
                    RuleResult(
                        check_id=cid,
                        rule_code=code,
                        outcome=RuleOutcome.NOT_CHECKED,
                        reason="внутренняя ошибка при проверке правила",
                    )
                )
        db.commit()


@celery_app.task(name="checks.sweep", soft_time_limit=900)
def sweep_task(check_id: str) -> None:
    """Поштучная проверка пунктов, затем итог. Сбой — правила «критично» не проверены."""
    s = services()
    try:
        _tracker(check_id).start(Stage.RULES, "Поштучная проверка пунктов")
        sweep_rules(s.session_factory, uuid.UUID(check_id), s.analysis)
    except Exception:
        log.exception("sweep_failed", check_id=check_id)
        try:
            fail_sweep(s.session_factory, uuid.UUID(check_id))
        except Exception:
            log.exception("sweep_fail_mark_failed", check_id=check_id)
    finalize_task(check_id)


@celery_app.task(name="checks.finalize", soft_time_limit=300)
def finalize_task(check_id: str) -> None:
    s = services()
    tracker = _tracker(check_id)
    try:
        tracker.done(Stage.RULES)
        tracker.start(Stage.FINALIZE)
        finalize_check(s.session_factory, uuid.UUID(check_id), s.ingest.max_pages)
        tracker.done(Stage.FINALIZE)
    except Exception:
        log.exception("finalize_failed", check_id=check_id)
        tracker.fail(Stage.FINALIZE, INTERNAL_ERROR)
        fail_check(s.session_factory, uuid.UUID(check_id), INTERNAL_ERROR)
