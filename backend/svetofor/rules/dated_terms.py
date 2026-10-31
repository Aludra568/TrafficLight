"""Срок, заданный датой окончания: «в срок: до 01 декабря 2026г.», «по 15.11.2026».

Дней в таком сроке нет, и пороговое правило без даты выдало бы «срок не найден числом». Дату
пересчитывает в дни правило (`from_date` у TERM-01); здесь — только где она и какая. Работает по
исходному тексту фрагмента: цитата — дословный текст документа.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass

from svetofor.rules.fact_rules import _NOT_SUBJECT, _OTHER_SUBJECT
from svetofor.rules.facts import SourceChunk

__all__ = ["DatedTerm", "delivery_end_dates"]

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_END_DATE = re.compile(
    r"(?<![а-яё])(?:до|по|не\s+позднее)\s*:?\s*(?P<day>\d{1,2})"
    rf"(?:\.(?P<month>\d{{1,2}})\.\s*|\s+(?P<month_name>{'|'.join(_MONTHS)})\s+)(?P<year>\d{{4}})",
    re.IGNORECASE,
)
# конец предложения: «;», точка перед заглавной (но не «г. Сыктывкар», «д. 5», «ул. Мира»),
# новый нумерованный пункт с новой строки
_SENTENCE_END = re.compile(
    r";|(?<![\s.,(«\"][а-яёА-ЯЁ])(?<!ул)(?<!обл)(?<!пр)(?<!стр)\.\s+(?=[А-ЯЁA-Z])"
    r"|\n\s*\d+(?:\.\d+)*\.?\s"
)
_DELIVERY_TOPIC = re.compile(
    rf"(?:^|[^а-яё]){_NOT_SUBJECT}(?:поставк|поставля|отгруз)", re.IGNORECASE
)
_OTHER_ACTION = re.compile(_OTHER_SUBJECT["delivery_term_days"])
_LOOKBACK = 300  # символов до даты, где ищется тема предложения


@dataclass(frozen=True)
class DatedTerm:
    """Срок поля, заданный датой окончания («до 01.12.2026»): дата и предложение с ней."""

    chunk: SourceChunk
    start: int  # смещения в тексте фрагмента — от начала предложения до конца даты
    end: int
    date: datetime.date
    raw: str  # дата как в документе: «01 декабря 2026», «01.12.2026»


def _end_date(match: re.Match[str]) -> datetime.date | None:
    name = match.group("month_name")
    month = _MONTHS.index(name.lower()) + 1 if name else int(match.group("month"))
    try:
        return datetime.date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:  # «31.02.2026» — опечатка или ошибка распознавания, не дата
        return None


def delivery_end_dates(chunks: Sequence[SourceChunk]) -> list[DatedTerm]:
    """Даты окончания поставки: «до / по / не позднее» + дата в предложении о поставке.

    Предложения без темы поставки («Контракт действует до 31.12.2026») и поставка по заявкам
    не берутся. Все найденные даты — в порядке документа.
    """
    found: list[DatedTerm] = []
    for chunk in chunks:
        text = chunk.text
        for match in _END_DATE.finditer(text):
            end_date = _end_date(match)
            if end_date is None:
                continue
            window = max(0, match.start() - _LOOKBACK)
            ends = list(_SENTENCE_END.finditer(text, window, match.start()))
            start = ends[-1].end() if ends else window
            sentence = text[start : match.end()]
            if not _DELIVERY_TOPIC.search(sentence):
                continue
            lowered = sentence.lower().replace("ё", "е")
            if "заявк" in lowered or _OTHER_ACTION.search(lowered):
                continue
            while start < match.start() and text[start].isspace():
                start += 1
            raw = text[match.start("day") : match.end("year")]
            found.append(DatedTerm(chunk, start, match.end(), end_date, raw))
    return found
