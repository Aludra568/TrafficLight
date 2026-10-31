"""Тесты сверки документов с учётом единиц измерения."""

from __future__ import annotations

from pathlib import Path

import pytest

from svetofor.db.enums import DocType, FindingKind, RuleOutcome, Severity
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import CrossDocumentRule
from svetofor.rules.results import CheckContext, DocumentView
from svetofor.rules.types import EngineDeps
from svetofor.rules.types.cross_document import CrossDocumentEvaluator, normalize_unit
from svetofor.rules.verify import QuoteVerifier

RULE_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
RULES = {r.id: r for r in load_ruleset_file(RULE_FILE).rules}
CON_02 = RULES["CON-02"]
assert isinstance(CON_02, CrossDocumentRule)
EVALUATOR = CrossDocumentEvaluator(EngineDeps(verifier=QuoteVerifier()))

SPEC_CHUNK = SourceChunk(
    id="s1",
    document_id="spec",
    section_path="ТЗ › 2. Объём",
    page_from=1,
    page_to=1,
    text="Объём закупки: 4 062,00 кв. м",
    char_start=0,
)

CONTRACT_CHUNK = SourceChunk(
    id="k1",
    document_id="contract",
    section_path="Проект контракта › 1. Предмет",
    page_from=1,
    page_to=1,
    text="Количество товара: 307 штук",
    char_start=0,
)


def make_fact(
    chunk: SourceChunk,
    value: float,
    quote: str,
    doc_type: DocType,
    unit: str | None = None,
    key: str = "quantity_total",
) -> FactValue:
    start = chunk.text.index(quote) if quote in chunk.text else 0
    return FactValue(
        key=key,
        value=value,
        unit=unit,
        raw=quote,
        quote=quote,
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        doc_type=doc_type,
        page_from=chunk.page_from,
        char_start=chunk.char_start + start,
        char_end=chunk.char_start + start + len(quote),
        score=100.0,
    )


def make_context(spec_fact: FactValue, contract_fact: FactValue) -> CheckContext:
    spec_facts = DocumentFacts(
        document_id="spec",
        doc_type=DocType.SPEC,
        facts={spec_fact.key: spec_fact},
    )
    contract_facts = DocumentFacts(
        document_id="contract",
        doc_type=DocType.CONTRACT,
        facts={contract_fact.key: contract_fact},
    )
    return CheckContext(
        documents=(
            DocumentView("spec", DocType.SPEC, "ТЗ.docx", (SPEC_CHUNK,)),
            DocumentView("contract", DocType.CONTRACT, "Проект контракта.docx", (CONTRACT_CHUNK,)),
        ),
        facts=(spec_facts, contract_facts),
        law="44-ФЗ",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("шт", "шт"),
        ("штук", "шт"),
        ("штука", "шт"),
        ("штуки", "шт"),
        ("ед", "шт"),
        ("единиц", "шт"),
        ("единица", "шт"),
        ("шт.", "шт"),
        ("  ШТУК  ", "шт"),
        ("кв м", "м2"),
        ("квм", "м2"),
        ("м2", "м2"),
        ("м²", "м2"),
        ("кв. метр", "м2"),
        ("кв. метра", "м2"),
        ("кв. метров", "м2"),
        ("квадратный метр", "м2"),
        ("куб м", "м3"),
        ("кубм", "м3"),
        ("м3", "м3"),
        ("м³", "м3"),
        ("куб. метр", "м3"),
        ("кубометр", "м3"),
        ("кг", "кг"),
        ("кг.", "кг"),
        ("килограмм", "кг"),
        ("т", "т"),
        ("т.", "т"),
        ("тонн", "т"),
        ("тонна", "т"),
        ("л", "л"),
        ("л.", "л"),
        ("литр", "л"),
        ("пог м", "м"),
        ("пог. м.", "м"),
        ("м", "м"),
        ("м.", "м"),
        ("погонный метр", "м"),
        ("метр", "м"),
        ("упак.", "упак"),
        ("рулон", "рулон"),
    ],
)
def test_cross_document_normalize_unit(raw: str, expected: str) -> None:
    assert normalize_unit(raw) == expected


def test_cross_document_different_units_not_checked() -> None:
    # 4 062,00 кв. м в ТЗ и 307 штук в контракте — разные единицы, не проверено без нарушения
    spec_fact = make_fact(SPEC_CHUNK, 4062.0, "4 062,00 кв. м", DocType.SPEC, unit="кв. м")
    contract_fact = make_fact(CONTRACT_CHUNK, 307.0, "307 штук", DocType.CONTRACT, unit="штук")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED
    assert outcome.findings == ()
    assert outcome.reason == "В документах разные единицы измерения: кв. м и штук — сравнить нельзя"


def test_cross_document_synonymous_units_different_numbers_violation() -> None:
    # Разные числа при синонимичных единицах («шт.» и «штук») — нарушение как раньше
    spec_fact = make_fact(SPEC_CHUNK, 100.0, "100", DocType.SPEC, unit="шт.")
    contract_fact = make_fact(CONTRACT_CHUNK, 200.0, "200", DocType.CONTRACT, unit="штук")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.outcome is RuleOutcome.VIOLATION
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert finding.kind is FindingKind.CONTRADICTION
    assert finding.severity is Severity.RED


def test_cross_document_same_numbers_synonymous_units_ok() -> None:
    # Одинаковые числа и синонимичные единицы («шт.» и «штук») — в порядке
    spec_fact = make_fact(SPEC_CHUNK, 500.0, "500", DocType.SPEC, unit="шт.")
    contract_fact = make_fact(CONTRACT_CHUNK, 500.0, "500", DocType.CONTRACT, unit="штук")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.outcome is RuleOutcome.OK
    assert outcome.findings == ()


def test_cross_document_one_fact_no_unit_equal_numbers_ok() -> None:
    # У одного факта нет единицы, числа одинаковые — в порядке
    spec_fact = make_fact(SPEC_CHUNK, 500.0, "500", DocType.SPEC, unit=None)
    contract_fact = make_fact(CONTRACT_CHUNK, 500.0, "500", DocType.CONTRACT, unit="штук")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    assert outcomes[0].outcome is RuleOutcome.OK
    assert outcomes[0].findings == ()


def test_cross_document_one_fact_no_unit_different_numbers_violation() -> None:
    # У одного факта нет единицы, числа разные — нарушение
    spec_fact = make_fact(SPEC_CHUNK, 500.0, "500", DocType.SPEC, unit=None)
    contract_fact = make_fact(CONTRACT_CHUNK, 600.0, "600", DocType.CONTRACT, unit="штук")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    assert outcomes[0].outcome is RuleOutcome.VIOLATION
    assert len(outcomes[0].findings) == 1


def test_cross_document_equal_numbers_different_units_not_checked() -> None:
    # Одинаковые числа при разных единицах (100 кг и 100 м) — несравнимы, не проверено
    spec_fact = make_fact(SPEC_CHUNK, 100.0, "100", DocType.SPEC, unit="кг")
    contract_fact = make_fact(CONTRACT_CHUNK, 100.0, "100", DocType.CONTRACT, unit="м")
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([CON_02], ctx)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED
    assert outcome.findings == ()
    assert outcome.reason == "В документах разные единицы измерения: кг и м — сравнить нельзя"


def test_cross_document_days_still_violation() -> None:
    # Сроки с разными единицами (calendar и working) по-прежнему дают противоречие
    con_01 = RULES["CON-01"]
    assert isinstance(con_01, CrossDocumentRule)
    spec_fact = make_fact(
        SPEC_CHUNK, 10.0, "10", DocType.SPEC, unit="calendar", key="delivery_term_days"
    )
    contract_fact = make_fact(
        CONTRACT_CHUNK, 10.0, "10", DocType.CONTRACT, unit="working", key="delivery_term_days"
    )
    ctx = make_context(spec_fact, contract_fact)

    evaluator = EVALUATOR
    outcomes = evaluator.evaluate([con_01], ctx)

    assert len(outcomes) == 1
    assert outcomes[0].outcome is RuleOutcome.VIOLATION
    assert len(outcomes[0].findings) == 1
