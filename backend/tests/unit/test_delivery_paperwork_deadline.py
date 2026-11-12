"""Срок оформления приёмки после поставки не является сроком поставки."""

from svetofor.rules.fact_rules import unparsed_terms
from tests.unit.test_green_safety import chunk

PAPERWORK = (
    "2.2. Поставщик не позднее 5 (пяти) рабочих дней со дня окончания поставки Товара "
    "формирует с использованием ЕИС, подписывает усиленной электронной подписью "
    "и размещает в ЕИС документ о приемке."
)


def test_acceptance_paperwork_is_not_an_unparsed_delivery_term():
    assert unparsed_terms("delivery_term_days", [chunk("paper", "d", PAPERWORK)]) == []


def test_paperwork_does_not_hide_a_separate_short_delivery_term():
    actual = chunk("delivery", "d", "Поставка Товара — по истечении 3 рабочих дней.")
    found = unparsed_terms("delivery_term_days", [actual, chunk("paper", "d", PAPERWORK)])
    assert len(found) == 1
    assert found[0].chunk.id == "delivery"


def test_paperwork_in_same_sentence_does_not_hide_delivery_term():
    text = (
        "Поставка Товара — по истечении 3 рабочих дней, а со дня окончания поставки "
        "Товара Поставщик формирует в ЕИС документ о приемке."
    )
    found = unparsed_terms("delivery_term_days", [chunk("delivery", "d", text)])
    assert [term.raw for term in found] == ["3 рабочих дней"]
