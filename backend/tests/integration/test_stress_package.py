"""Стресс-пакет с ловушками — через настоящий конвейер, без ключа модели.

То, что требует OCR и LibreOffice, проверяется только там, где они есть (Docker-образ);
локально такие документы должны быть честно помечены, а остальные ловушки — сработать.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import DocumentStatus, RuleOutcome, Severity
from svetofor.db.models import Check, Document, Finding, RuleResult
from svetofor.eval.stress import CONTRACT_NAME, NOTICE_NAME, REVISED_NAME, generate_stress
from svetofor.pipeline.finalize import finalize_check, start_check
from svetofor.pipeline.ingest import IngestConfig, create_check, prepare_package, process_document
from svetofor.pipeline.orchestrator import (
    AnalysisDeps,
    check_rules,
    embed_chunks,
    evaluate_rules,
    extract_document_facts,
    load_context,
    rule_batches,
)
from svetofor.pipeline.unpack import find_bsdtar
from svetofor.rules.facts import merge_card
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.rules.verify import QuoteVerifier
from tests.support.fake_models import StemEmbedder
from tests.support.rulesets import RULES_FILE

pytestmark = pytest.mark.pg


def _run(factory: sessionmaker[Session], tmp_path: Path) -> object:
    archive = generate_stress(tmp_path / "stress")
    config = IngestConfig(packages_dir=tmp_path / "packages", bsdtar=find_bsdtar())
    analysis = AnalysisDeps(QuoteVerifier(), None, StemEmbedder())  # без модели
    with factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db,
            upload=archive,
            original_name=archive.name,
            config=config,
            ruleset_version_id=version.id,
        )
        db.commit()
    start_check(factory, check.id)
    document_ids = prepare_package(factory, check.id, config)
    for document_id in document_ids:
        process_document(factory, document_id, config)
    embed_chunks(factory, check.id, StemEmbedder())
    for document_id in document_ids:
        extract_document_facts(factory, check.id, document_id, analysis)
    with factory() as db:
        rules = [r for _, r in check_rules(db, db.get(Check, check.id))]  # type: ignore[arg-type]
    for codes in rule_batches(rules):
        evaluate_rules(factory, check.id, analysis, codes)
    finalize_check(factory, check.id)
    return check.id


def test_stress_package_traps(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    check_id = _run(session_factory, tmp_path)
    docker = shutil.which("soffice") and shutil.which("tesseract")
    with session_factory() as db:
        documents = {d.filename: d for d in db.scalars(select(Document))}
        # архив 7z (или ZIP) и вложенный ZIP распакованы полностью
        assert set(documents) == {
            NOTICE_NAME,
            CONTRACT_NAME,
            "Описание объекта закупки (скан).pdf",
            REVISED_NAME,
        }
        findings = {f.rule_code: f for f in db.scalars(select(Finding))}
        results = {r.rule_code: r for r in db.scalars(select(RuleResult))}
        # подмена букв в наименовании
        assert "Пoставка" in (findings["SUB-03"].quote or "")
        # срок оплаты прописью → красный, без модели
        assert findings["PAY-01"].severity is Severity.RED
        assert "сорока пяти" in (findings["PAY-01"].quote or "")
        # устаревшая «банковская гарантия»
        assert "банковской гарантией" in (findings["SEC-05"].quote or "")
        # белый текст «рисков нет» — показан, а не послушан
        hidden = findings["CON-03"]
        assert "рисков для поставщика нет" in (hidden.quote or "") and hidden.highlights
        # 5 % в извещении против 10 % в контракте
        assert results["CON-04"].outcome is RuleOutcome.VIOLATION
        # «кракозябры» на странице 2 извещения починены: номер закупки и НМЦК в карточке
        check = db.get(Check, check_id)
        assert check is not None
        card = merge_card(load_context(db, check).facts)
        assert card["procurement_number"].value == "0373100012326000777"
        assert card["nmck_rub"].value == 1250000
        spec = documents["Описание объекта закупки (скан).pdf"]
        revised = documents[REVISED_NAME]
        if docker:
            assert results["CON-01"].outcome is RuleOutcome.VIOLATION  # после OCR
            assert revised.error and "исправления" in revised.error
            assert card["quantity_total"].value == 500  # принятая правка, а не «600 500»
        else:
            # без OCR и LibreOffice — честно: скан без текста, DOCX не сконвертирован
            assert results["CON-01"].outcome is RuleOutcome.NOT_CHECKED
            assert spec.error is not None
            assert revised.status is DocumentStatus.FAILED and "LibreOffice" in (
                revised.error or ""
            )
