"""Сквозная проверка без Celery: архив → документы → эмбеддинги → карточка → все правила → итог."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.core.config import LLMMode
from svetofor.db.enums import CheckStatus, FindingKind, FindingStatus, Light, RuleOutcome
from svetofor.db.models import Check, Chunk, Fact, Finding, RuleResult
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.pipeline.finalize import fail_check, finalize_check, start_check
from svetofor.pipeline.ingest import IngestConfig, create_check, prepare_package, process_document
from svetofor.pipeline.orchestrator import (
    AnalysisDeps,
    embed_chunks,
    evaluate_rules,
    extract_document_facts,
    rule_batches,
)
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.rules.verify import QuoteVerifier
from tests.support.fake_models import RoutingBackend, StemEmbedder, fact
from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup
from tests.support.rulesets import RULE_COUNT

pytestmark = pytest.mark.pg
RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"

CONTRACT = """
!! ПРОЕКТ КОНТРАКТА
Контракт заключается в соответствии с Федеральным законом № 44-ФЗ.
**1. ПРЕДМЕТ КОНТРАКТА**
1.1. Поставщик обязуется поставлять бумагу по мере необходимости
Заказчика на основании заявок.
**2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ**
2.1. Оплата производится в течение 15 рабочих дней с даты подписания
документа о приёмке. Аванс не предусмотрен.
**3. СРОКИ ПОСТАВКИ**
3.1. Срок поставки — 10 рабочих дней с даты заявки.
===
**4. ОТВЕТСТВЕННОСТЬ СТОРОН**
4.1. За просрочку поставщик уплачивает пеню в размере 1/100 цены
контракта за каждый день просрочки.
"""
SPEC = """
!! ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ
**1. Требования к поставке**
1.1. Срок поставки: 10 календарных дней с даты заключения контракта.
"""
FACTS = {
    "Проект контракта": [
        fact("law", "44-ФЗ", "в соответствии с Федеральным законом № 44-ФЗ"),
        fact(
            "payment_term_working_days",
            15,
            "в течение 15 рабочих дней с даты подписания",
            "working",
        ),
        fact("advance_percent", 0, "Аванс не предусмотрен"),
        fact("delivery_term_days", 10, "Срок поставки — 10 рабочих дней с даты заявки", "working"),
    ],
    "Описание объекта закупки": [
        fact("delivery_term_days", 10, "Срок поставки: 10 календарных дней", "calendar"),
    ],
}
VIOLATIONS = {
    "LIA-03": (
        "пеню в размере 1/100 цены контракта за каждый день просрочки",
        "Пеня 1/100 цены в день значительно выше законной.",
    ),
}


def build_upload(tmp_path: Path) -> Path:
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, markup in (
            ("Проект контракта.pdf", CONTRACT),
            ("Описание объекта закупки.pdf", SPEC),
        ):
            pdf = build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / f"{len(name)}.pdf")
            zf.write(pdf, name)
    return archive


def deps(backend: RoutingBackend | None, embedder: StemEmbedder | None = None) -> AnalysisDeps:
    gateway = None
    if backend is not None:
        gateway = LLMGateway(
            mode=LLMMode.LIVE, model_main="m", model_fast="m", backend=backend, cache=MemoryCache()
        )
    return AnalysisDeps(
        verifier=QuoteVerifier(), gateway=gateway, embedder=embedder or StemEmbedder()
    )


def run_check(factory: sessionmaker[Session], tmp_path: Path, analysis: AnalysisDeps) -> Any:
    config = IngestConfig(packages_dir=tmp_path / "packages")
    with factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db,
            upload=build_upload(tmp_path),
            original_name="Закупка.zip",
            config=config,
            ruleset_version_id=version.id,
        )
        db.commit()
    start_check(factory, check.id)
    document_ids = prepare_package(factory, check.id, config)
    for document_id in document_ids:
        process_document(factory, document_id, config)
    assert analysis.embedder is not None
    embed_chunks(factory, check.id, analysis.embedder)  # type: ignore[arg-type]
    for document_id in document_ids:
        extract_document_facts(factory, check.id, document_id, analysis)
    with factory() as db:
        from svetofor.pipeline.orchestrator import check_rules

        rules = [r for _, r in check_rules(db, db.get(Check, check.id))]  # type: ignore[arg-type]
    for codes in rule_batches(rules):
        evaluate_rules(factory, check.id, analysis, codes)
    finalize_check(factory, check.id)
    return check.id


def test_parallel_facts_do_not_deadlock(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Факты документов пишутся параллельно (worker-llm, потоки): вставка фактов берёт по
    внешнему ключу разделяемую блокировку строки проверки, и если обе задачи вставили факты
    до `FOR UPDATE` счётчиков, они ждали друг друга — PostgreSQL обрывал одну
    (DeadlockDetected, «Внутренняя ошибка сервиса», в Docker 20.10.2026). Барьер держит обе
    задачи ровно в этом окне: с правкой вторая ждёт блокировку строки до вставки."""
    import contextlib
    import threading

    from svetofor.pipeline import orchestrator

    analysis = deps(RoutingBackend(FACTS, VIOLATIONS))
    config = IngestConfig(packages_dir=tmp_path / "packages")
    with session_factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db,
            upload=build_upload(tmp_path),
            original_name="Закупка.zip",
            config=config,
            ruleset_version_id=version.id,
        )
        db.commit()
    start_check(session_factory, check.id)
    document_ids = prepare_package(session_factory, check.id, config)
    for document_id in document_ids:
        process_document(session_factory, document_id, config)
    assert len(document_ids) == 2

    barrier = threading.Barrier(2)
    original = orchestrator._update_fact_counters

    def counters_after_both_inserted(db: Session, check_id: Any, result: Any) -> None:
        db.flush()  # факты вставлены — блокировка по внешнему ключу взята
        # с правкой вторая задача ждёт блокировку строки и до барьера не доходит
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=3)
        original(db, check_id, result)

    monkeypatch.setattr(orchestrator, "_update_fact_counters", counters_after_both_inserted)
    errors: list[BaseException] = []

    def run(document_id: Any) -> None:
        try:
            extract_document_facts(session_factory, check.id, document_id, analysis)
        except BaseException as exc:  # ошибку из потока проверяет тест ниже
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(d,)) for d in document_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    with session_factory() as db:
        stored = db.get(Check, check.id)
        assert stored is not None and stored.counters.get("facts_errors") == {}
        documents = set(db.scalars(select(Fact.document_id).where(Fact.check_id == check.id)))
        assert documents == set(document_ids)


def test_full_check(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    backend = RoutingBackend(FACTS, VIOLATIONS)
    check_id = run_check(session_factory, tmp_path, deps(backend))
    with session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None
        assert check.status is CheckStatus.DONE and check.light is Light.RED
        assert (
            check.law_detected == "44-ФЗ" and check.risk_index is not None and check.risk_index > 0
        )
        results = {
            r.rule_code: r
            for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
        }
        assert len(results) == RULE_COUNT
        assert results["PAY-01"].outcome is RuleOutcome.VIOLATION  # 15 > 7 раб. дн.
        assert results["VOL-01"].outcome is RuleOutcome.VIOLATION  # «по мере необходимости»
        assert results["CON-01"].outcome is RuleOutcome.VIOLATION  # календарные против рабочих
        assert results["LIA-03"].outcome is RuleOutcome.VIOLATION  # модель + проверка цитаты
        # срока приёмки нет: обязательное условие — «проверьте вручную», а не «не проверено»
        assert results["ACC-01"].outcome is RuleOutcome.ABSENT
        findings = {
            f.rule_code: f for f in db.scalars(select(Finding).where(Finding.check_id == check_id))
        }
        pay = findings["PAY-01"]
        assert pay.section_path == "Проект контракта › 2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ › 2.1"
        assert pay.highlights and pay.highlights[0]["page"] == 1
        penalty = findings["LIA-03"]
        assert penalty.page_from == 2 and penalty.quote is not None and "\n" in penalty.quote
        # красное нарушение модели перепроверено вторым мнением
        assert (penalty.related or {})["review"]["decision"] == "confirmed"
        contradiction = findings["CON-01"]
        assert contradiction.kind is FindingKind.CONTRADICTION and contradiction.document_id is None
        assert len({h["document_id"] for h in contradiction.highlights}) == 2  # оба документа
        assert (
            check.counters["findings"] == len(findings)
            and check.counters["rejected_unverified"] == 0
        )
        assert db.scalar(select(Fact).where(Fact.key == "payment_term_working_days")) is not None
        assert all(c.embedding is not None for c in db.scalars(select(Chunk)))
    llm_calls = [r for r in backend.requests if "Проверь фрагменты" in r.messages[1].content]
    # 8 групп модели — 8 вызовов. REQ-01 смотрит извещение и требования к заявке — их в пакете
    # нет: правило честно «не проверено»; его группу проверяет REQ-02 (контракт и ТЗ)
    assert len(rule_batches(load_ruleset_file(RULES_FILE).rules)) - 1 == 8
    assert len(llm_calls) == 8
    assert results["REQ-01"].reason == "в пакете нет документа: извещение, требования к заявке"


def test_without_model_everything_honest(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    check_id = run_check(session_factory, tmp_path, deps(None, StemEmbedder(fail=True)))
    with session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None and check.status is CheckStatus.DONE_WITH_WARNINGS
        results = {
            r.rule_code: r
            for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
        }
        assert results["VOL-01"].outcome is RuleOutcome.VIOLATION  # шаблоны работают без модели
        assert results["LIA-03"].outcome is RuleOutcome.NOT_CHECKED
        assert results["LIA-03"].reason == "нет доступа к модели"
        # карточку заполняет канал грамматик: пороги и сверка документов работают без модели
        assert results["PAY-01"].outcome is RuleOutcome.VIOLATION
        assert results["CON-01"].outcome is RuleOutcome.VIOLATION
        payment = db.scalar(select(Fact).where(Fact.key == "payment_term_working_days"))
        assert payment is not None and payment.source == "rules" and payment.conflict is None
        assert check.counters["facts_errors"]  # модель не участвовала — это видно
        # «не проверено» из-за отсутствия модели отличается от «не проверено» по существу
        assert results["LIA-03"].details == {"error": "unavailable"}
        # нет извещения — это не сбой; правило не «критично», зелёному не мешает
        assert results["REQ-01"].details == {"missing_documents": ["notice", "bid_requirements"]}
        assert check.counters["rules_failed"] >= 1


def test_invented_quote_is_counted_not_shown(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    class Liar(RoutingBackend):
        def _verdict(self, code: str, prompt: str) -> dict[str, Any]:
            if code == "LIA-01":
                return {
                    "rule_id": code,
                    "verdict": "violation",
                    "chunk_id": "F1",
                    "quote": "штраф составляет 50 процентов цены контракта",
                    "explanation": "x",
                }
            return super()._verdict(code, prompt)

    check_id = run_check(session_factory, tmp_path, deps(Liar(FACTS, VIOLATIONS)))
    with session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None and check.counters["rejected_unverified"] == 1
        lia1 = db.scalar(
            select(Finding).where(Finding.check_id == check_id, Finding.rule_code == "LIA-01")
        )
        assert (
            lia1 is not None
            and lia1.status is FindingStatus.REJECTED_UNVERIFIED
            and lia1.highlights == []
        )


def test_nothing_readable_is_gray_not_green(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Ни один документ не открылся — замечаний нет, но это не «рисков нет»."""
    config = IngestConfig(packages_dir=tmp_path / "packages")
    upload = tmp_path / "broken.zip"
    with zipfile.ZipFile(upload, "w") as zf:
        zf.writestr("Проект контракта.pdf", b"%PDF-1.4 broken")
    with session_factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db, upload=upload, original_name="x.zip", config=config, ruleset_version_id=version.id
        )
        db.commit()
    start_check(session_factory, check.id)
    for document_id in prepare_package(session_factory, check.id, config):
        process_document(session_factory, document_id, config)
    with session_factory() as db:
        from svetofor.pipeline.orchestrator import check_rules

        rules = [r for _, r in check_rules(db, db.get(Check, check.id))]  # type: ignore[arg-type]
    for codes in rule_batches(rules):
        evaluate_rules(session_factory, check.id, deps(None), codes)
    finalize_check(session_factory, check.id)
    with session_factory() as db:
        saved = db.get(Check, check.id)
        assert saved is not None
        assert saved.light is Light.GRAY and saved.risk_index is None
        assert saved.status is CheckStatus.DONE_WITH_WARNINGS


def test_fail_check_records_reason(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    config = IngestConfig(packages_dir=tmp_path / "packages")
    upload = tmp_path / "x.zip"
    upload.write_bytes(b"not zip")
    with session_factory() as db:
        check = create_check(
            db, upload=upload, original_name="x.zip", config=config, ruleset_version_id=None
        )
        db.commit()
    start_check(session_factory, check.id)
    fail_check(session_factory, check.id, "Архив повреждён или это не ZIP")
    with session_factory() as db:
        saved = db.get(Check, check.id)
        assert saved is not None and saved.status is CheckStatus.FAILED
        assert saved.error == "Архив повреждён или это не ZIP" and saved.finished_at is not None
