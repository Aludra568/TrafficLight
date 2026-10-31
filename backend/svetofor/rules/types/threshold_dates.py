"""Оценка срока в днях, когда он задан датой окончания («поставка до 01.12.2026»).

Дата начала исполнения в документах обычно «с даты заключения контракта», а сама дата заранее не
известна. Поэтому отсчёт идёт от окончания подачи заявок (карточка извещения) плюс запас
`lag_days` на протокол и подписание. Запас берётся с избытком: реальный срок у поставщика обычно
не меньше оценки, а сомнительный случай (несколько дат, нет даты подачи, оценка ≤ 0) остаётся
прежним «проверьте вручную».
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from svetofor.db.enums import DocType
from svetofor.rules.dated_terms import DatedTerm, delivery_end_dates
from svetofor.rules.facts import FactValue
from svetofor.rules.model import DATED_FACTS, ThresholdRule
from svetofor.rules.results import CheckContext

__all__ = ["DateEstimate", "estimate_from_date"]

_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@dataclass(frozen=True)
class DateEstimate:
    fact: FactValue  # оценка в календарных днях, цитата — предложение с датой
    term: DatedTerm
    note: str  # как посчитано — в пояснение замечания и в «почему зелёный»


def _parse(value: object) -> datetime.date | None:
    match = _DATE.search(str(value or ""))
    if match is None:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def estimate_from_date(rule: ThresholdRule, ctx: CheckContext) -> DateEstimate | None:
    """Оценка срока по дате окончания или `None` — тогда правило поступает как без даты."""
    if rule.from_date is None or rule.fact not in DATED_FACTS:
        return None
    documents = ctx.documents_of(rule.applies_to)
    found = [(d, t) for d in documents for t in delivery_end_dates(d.chunks)]
    if len({t.date for _, t in found}) != 1:
        return None  # даты нет или их несколько разных — какая из них срок, не угадываем
    start_fact = ctx.card_for([DocType.NOTICE]).get(rule.from_date.start_fact)
    start = _parse(start_fact.value) if start_fact is not None else None
    if start is None:
        return None
    document, term = found[0]
    lag = rule.from_date.lag_days
    days = (term.date - start).days - lag
    if days <= 0:
        return None
    chunk = term.chunk
    fact = FactValue(
        key=rule.fact,
        value=float(days),
        unit="calendar",
        raw=term.raw,
        quote=chunk.text[term.start : term.end],
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        doc_type=document.doc_type,
        page_from=chunk.page_from,
        char_start=chunk.char_start + term.start,
        char_end=chunk.char_start + term.end,
        score=100.0,
        source="rules",
    )
    note = (
        f"Срок поставки задан датой (до {term.date:%d.%m.%Y}). Дни посчитаны от окончания подачи "
        f"заявок ({start:%d.%m.%Y}) с запасом {lag} дн. на заключение контракта — это оценка, "
        "проверьте по документу."
    )
    return DateEstimate(fact, term, note)
