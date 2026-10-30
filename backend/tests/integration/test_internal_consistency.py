"""Противоречия внутри одного документа (вопрос участников организаторам, 2026-10-08).

«В одной части документа "закупаем 5000 яблок", в другой — "3000"»: сверка документов между
собой сравнивала только главное значение поля, и договор с 5000 в п. 1.1 и 3000 в п. 8.1 при
ТЗ на 5000 давал зелёный. Правило CON-07 (тип `internal_consistency`) сверяет все значения
поля, которое в документе должно быть одним: количество, НМЦК, обеспечение, аванс.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from svetofor.core.config import LLMMode, get_settings
from svetofor.db.enums import FindingKind, Light, RuleOutcome
from svetofor.eval.run import run_set
from svetofor.eval.synthetic import build_package, load_templates, write_package
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.pipeline.ingest import IngestConfig
from svetofor.pipeline.orchestrator import AnalysisDeps
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.rules.verify import QuoteVerifier
from svetofor.tasks import checks
from tests.integration.test_eval_run import RULES_FILE, HonestFake
from tests.integration.test_redteam_green import CLEAN_SPEC, HonestModel, contract, pdf, run
from tests.support.fake_models import StemEmbedder

pytestmark = pytest.mark.pg

SPEC_5000 = CLEAN_SPEC.replace("500 пачек", "5000 пачек")
CONTRACT_5000 = contract(
    **{"1.1": "Поставщик обязуется поставить бумагу офисную в количестве 5000 пачек."}
)


def _files(extra_clause: str) -> dict[str, object]:
    text = CONTRACT_5000 + f"**8. СПЕЦИФИКАЦИЯ**\n8.1. {extra_clause}\n"
    return {"Проект контракта.pdf": pdf(text), "Описание объекта закупки.pdf": pdf(SPEC_5000)}


def test_two_quantities_in_one_contract_are_a_contradiction(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    files = _files("Товар поставляется в количестве 3000 пачек.")
    light, results, findings = run(session_factory, tmp_path, files, HonestModel())  # type: ignore[arg-type]
    assert results["CON-07"].outcome is RuleOutcome.VIOLATION
    finding = next(f for f in findings if f.rule_code == "CON-07")
    assert finding.kind is FindingKind.CONTRADICTION
    message = finding.message or ""
    assert "5\xa0000 пачек" in message and "3\xa0000 пачек" in message  # неразрывный пробел
    quotes = [e["quote"] for e in (finding.related or {}).get("evidence", [])]
    assert any("5000" in q for q in quotes) and any("3000" in q for q in quotes)
    assert light is not Light.GREEN


def test_partial_lot_is_not_a_contradiction(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Поставка партиями: «первая партия в количестве 3000 пачек» — часть, а не другое число."""
    files = _files("Первая партия товара поставляется в количестве 3000 пачек.")
    _, results, _ = run(session_factory, tmp_path, files, HonestModel())  # type: ignore[arg-type]
    assert results["CON-07"].outcome is RuleOutcome.OK


def test_consistent_values_are_ok_with_evidence(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    files = _files("Товар поставляется в количестве 5000 пачек.")
    _, results, _ = run(session_factory, tmp_path, files, HonestModel())  # type: ignore[arg-type]
    assert results["CON-07"].outcome is RuleOutcome.OK
    quotes = [e["quote"] for e in (results["CON-07"].details or {}).get("evidence", [])]
    assert any("5000 пачек" in q for q in quotes)  # «почему в порядке» — с цитатой значения


def test_synthetic_contradiction_found_and_lots_trap_not_flagged(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Оценка на синтетике: syn-017 — противоречие из примера организаторов, syn-010 — ловушка
    «первая партия 20 из 50 штук». Одно верное замечание CON-07, ни одного ложного."""
    rules = load_ruleset_file(RULES_FILE).rules
    documents, slots, catalog = load_templates()
    set_dir = tmp_path / "synthetic"
    for index, spec in enumerate(catalog["packages"], start=1):
        if spec["id"] in ("syn-010", "syn-017"):
            package = build_package(spec, index, documents, slots, catalog["subjects"])
            write_package(package, rules, set_dir / spec["id"], "pdf")
    with session_factory() as db:
        import_ruleset(db, load_ruleset_file(RULES_FILE))
        db.commit()
    gateway = LLMGateway(
        mode=LLMMode.LIVE,
        model_main="fake",
        model_fast="fake",
        backend=HonestFake({}, {}),
        cache=MemoryCache(),
    )
    services = checks.Services(
        settings=get_settings(),
        session_factory=session_factory,
        ingest=IngestConfig(packages_dir=tmp_path / "packages"),
        analysis=AnalysisDeps(QuoteVerifier(), gateway, StemEmbedder()),
    )
    report = run_set(
        services, set_dir, tmp_path / "results", ruleset_code="44fz-supplier-base", model="fake"
    )
    assert report["per_rule"]["CON-07"]["tp"] == 1
    assert report["per_rule"]["CON-07"]["fp"] == 0
    assert report["per_rule"]["CON-07"]["fn"] == 0
