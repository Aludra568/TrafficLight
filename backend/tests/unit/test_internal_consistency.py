"""`internal_consistency`: какие разные значения поля — противоречие, а какие — законная разница."""

from __future__ import annotations

import pytest

from svetofor.db.enums import DocType
from svetofor.rules.fact_rules import extract_rule_facts
from svetofor.rules.facts import FactValue, SourceChunk
from svetofor.rules.model import InternalConsistencyRule
from svetofor.rules.types.internal_consistency import contradiction


def fact(key: str, text: str, doc_type: DocType = DocType.CONTRACT) -> FactValue:
    chunk = SourceChunk("c1", "d1", "1", 1, 1, text, 0)
    found = extract_rule_facts("d1", doc_type, [chunk]).facts[key]
    return found


def test_two_quantities_contradict() -> None:
    quantity = fact(
        "quantity_total",
        "1.1. Поставщик обязуется поставить бумагу в количестве 5000 пачек. "
        "8.1. Товар поставляется в количестве 3000 пачек.",
    )
    other = contradiction(quantity)
    assert other is not None and (quantity.value, other.value) == (5000.0, 3000.0)
    assert "3000 пачек" in other.quote


def test_same_unit_in_another_form_still_contradicts() -> None:
    quantity = fact(
        "quantity_total",
        "1.1. Поставка в количестве 5000 пачек. 8.1. Поставка в количестве 3000 пачки.",
    )
    assert contradiction(quantity) is not None


def test_two_percents_of_security_contradict() -> None:
    security = fact(
        "contract_security_percent",
        "Размер обеспечения исполнения контракта составляет 5 процентов цены контракта. "
        "Обеспечение исполнения контракта предоставляется в размере 10 процентов цены контракта.",
    )
    other = contradiction(security)
    assert other is not None and other.value == 10.0


@pytest.mark.parametrize(
    "text",
    [
        # партия — часть общего количества
        "Поставка в количестве 5000 пачек. Первая партия поставляется в количестве 3000 пачек.",
        "Поставка в количестве 5000 пачек, в том числе в количестве 3000 пачек до 1 марта.",
        # другая единица — пересчёт, а не другое число
        "Поставка в количестве 5000 пачек. Упаковка: в количестве 500 коробок по 10 пачек, "
        "всего в количестве 50 комплектов.",
        # список позиций: три разных значения
        "Бумага А4 в количестве 300 пачек. Бумага А3 в количестве 200 пачек. "
        "Бумага цветная в количестве 100 пачек.",
        # общее количество и две равные партии
        "Поставка в количестве 5000 пачек. Поставка в количестве 2500 пачек ежемесячно.",
    ],
)
def test_parts_lists_and_other_units_are_not_contradictions(text: str) -> None:
    assert contradiction(fact("quantity_total", text)) is None


@pytest.mark.parametrize(
    "text",
    [
        # предел из закона рядом с размером обеспечения
        "Размер обеспечения исполнения контракта составляет 5 процентов цены контракта. "
        "Обеспечение исполнения контракта устанавливается в размере не более 30 процентов.",
        "Размер обеспечения исполнения контракта составляет 5 процентов цены контракта. "
        "Обеспечение исполнения контракта устанавливается от 0,5 % до 30 % цены контракта.",
    ],
)
def test_legal_limits_and_ranges_are_not_contradictions(text: str) -> None:
    assert contradiction(fact("contract_security_percent", text)) is None


def test_single_value_has_no_contradiction() -> None:
    assert contradiction(fact("quantity_total", "Поставка в количестве 5000 пачек.")) is None


def test_rule_accepts_only_known_numeric_facts() -> None:
    base = {
        "id": "CON-99",
        "title": "Тест",
        "category": "consistency",
        "law": ["44-ФЗ"],
        "applies_to": ["contract"],
        "type": "internal_consistency",
        "severity": "yellow",
        "message": "{first} и {second}",
        "why_risk": "риск",
        "recommendation": "запрос",
    }
    assert InternalConsistencyRule.model_validate({**base, "facts": ["quantity_total"]})
    with pytest.raises(ValueError, match="Неизвестный факт"):
        InternalConsistencyRule.model_validate({**base, "facts": ["apples"]})
    with pytest.raises(ValueError, match="только числа"):
        InternalConsistencyRule.model_validate({**base, "facts": ["law"]})
    with pytest.raises(ValueError):
        InternalConsistencyRule.model_validate({**base, "facts": []})
