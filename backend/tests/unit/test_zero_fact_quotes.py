"""Ноль в карточке требует подтверждения именно соответствующего факта."""

from dataclasses import replace

import pytest

from svetofor.db.enums import DocType
from svetofor.rules.facts import value_matches_quote
from svetofor.rules.facts_catalog import FACTS
from tests.unit.test_facts import CHUNKS, extractor, fact


@pytest.mark.parametrize("quote, accepted", [("Аванс 30%", False), ("Аванс 0%", True)])
@pytest.mark.parametrize("value", [0, 0.0])
def test_zero_advance_must_match_quote(quote, accepted, value):
    assert value_matches_quote(FACTS["advance_percent"], value, quote) is accepted


@pytest.mark.parametrize("percent, accepted", [(30, False), (0, True)])
def test_extracted_zero_uses_existing_rejection(percent, accepted):
    quote = f"Аванс {percent}% от цены контракта"
    fx, _ = extractor(fact("advance_percent", 0, quote))
    result = fx.extract(
        document_id="doc-1",
        doc_type=DocType.CONTRACT,
        doc_label="Контракт",
        chunks=[replace(CHUNKS[0], text=quote)],
    )
    assert ("advance_percent" in result.facts) is accepted
    assert bool(result.rejected) is not accepted


@pytest.mark.parametrize(
    "key, quote, accepted",
    [
        ("advance_percent", "Аванс не предусмотрен", True),
        ("advance_percent", "Без аванса", True),
        ("contract_security_percent", "Обеспечение исполнения контракта не требуется", True),
        ("bid_security_percent", "Обеспечение заявки не установлено", True),
        ("warranty_security_percent", "Обеспечение гарантийных обязательств не требуется", True),
        ("payment_term_working_days", "Аванс не предусмотрен", False),
        ("advance_percent", "Обеспечение заявки не установлено", False),
        ("advance_percent", "Размер аванса указан в графике", False),
    ],
)
def test_zero_absence_requires_matching_fact_type(key, quote, accepted):
    assert value_matches_quote(FACTS[key], 0.0, quote) is accepted


@pytest.mark.parametrize(
    "quote",
    [
        "Аванс составляет 30%, обеспечение заявки составляет 0%.",
        "Аванс составляет тридцать процентов, обеспечение заявки составляет 0%.",
        "Аванс 0%, обеспечение заявки 5%.",
    ],
)
def test_zero_with_other_numbers_is_ambiguous(quote):
    assert not value_matches_quote(FACTS["advance_percent"], 0.0, quote)
