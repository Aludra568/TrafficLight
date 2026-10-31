import datetime

import pytest

from svetofor.rules.dated_terms import delivery_end_dates
from svetofor.rules.facts import SourceChunk


def chunk_of(text: str) -> SourceChunk:
    return SourceChunk("c1", "d1", "Контракт", 1, 1, text, 1000)


@pytest.mark.parametrize(
    ("text", "expected", "raw"),
    [
        # real-004, п. 3.1 контракта: сокращения «г.», «ул.», «д.» не рвут предложение
        (
            "3.1. Поставщик осуществляет поставку Товара по адресу: г. Сыктывкар, ул. Мира, "
            "д.19, в срок: до 01 декабря 2026г.",
            datetime.date(2026, 12, 1),
            "01 декабря 2026",
        ),
        ("Срок поставки товара: до 01.12.2026 включительно.", datetime.date(2026, 12, 1), None),
        ("Срок поставки товара: по 15.11.2026.", datetime.date(2026, 11, 15), None),
        ("Товар поставляется не позднее 20.11.2026 года.", datetime.date(2026, 11, 20), None),
        # таблица ТЗ: заголовок и значение на разных строках
        ("Срок поставки товара\nДо 01.12.2026 включительно", datetime.date(2026, 12, 1), None),
    ],
)
def test_finds_delivery_end_date(text: str, expected: datetime.date, raw: str | None) -> None:
    chunk = chunk_of(text)
    terms = delivery_end_dates([chunk])
    assert [t.date for t in terms] == [expected]
    term = terms[0]
    assert term.raw in chunk.text[term.start : term.end]
    if raw is not None:
        assert term.raw == raw


@pytest.mark.parametrize(
    "text",
    [
        "Контракт действует до 31.12.2026.",
        "Обеспечение возвращается до 31.12.2026.",
        "Поставка осуществляется по заявкам до 31.12.2026.",
        "Срок поставки товара: до 31.02.2026.",
        # тема поставки в прошлом предложении — не тема этого
        "Поставка товара осуществляется в течение 10 дней. Контракт действует до 31.12.2026.",
        # поставка — только точка отсчёта
        "Оплата производится по 31.12.2026 с даты поставки товара.",
    ],
)
def test_ignores_dates_of_other_things(text: str) -> None:
    assert delivery_end_dates([chunk_of(text)]) == []
