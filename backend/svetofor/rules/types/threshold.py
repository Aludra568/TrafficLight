"""`threshold` — сравнение факта карточки с порогом.

Условия проверяются сверху вниз, первое совпадение задаёт цвет. Срок «в рабочих днях»,
указанный в документе в календарных, сравнивается по приближению ×5/7 — и замечание
честно объясняет пересчёт. Факта нет из-за сбоя модели — «не проверено», а не «всё хорошо».
Каналы карточки разошлись — цвет по худшему из двух значений, оба места в документе показаны.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from svetofor.db.enums import DocType, FindingKind, RuleOutcome, Severity
from svetofor.rules.conditions import parse_condition
from svetofor.rules.fact_rules import (
    TERM_PATTERNS,
    UnreadableTerm,
    unparsed_terms,
    unreadable_terms,
)
from svetofor.rules.facts import FactValue, numbers_in, value_matches_quote
from svetofor.rules.facts_catalog import FACTS, FactKind, display_value
from svetofor.rules.model import ThresholdCondition, ThresholdRule
from svetofor.rules.numerals import spelled_numbers
from svetofor.rules.results import (
    CheckContext,
    Evidence,
    FindingDraft,
    RuleOutcomeDraft,
    not_checked,
    ok_with_evidence,
)
from svetofor.rules.templates import render
from svetofor.rules.types import RuleEvaluator, register
from svetofor.rules.verify import normalize

WORKING_PER_CALENDAR = 5 / 7


def comparable(fact: FactValue) -> tuple[float | bool | str, bool]:
    """Значение для сравнения с порогом и признак пересчёта календарных дней в рабочие."""
    calendar_days = (
        FACTS[fact.key].kind is FactKind.NUMBER
        and isinstance(fact.value, float)
        and fact.key.endswith("_working_days")
        and fact.unit == "calendar"
    )
    if calendar_days and isinstance(fact.value, float):
        return round(fact.value * WORKING_PER_CALENDAR, 1), True
    return fact.value, False


def fact_evidence(fact: FactValue, ctx: CheckContext) -> Evidence:
    document = ctx.document(fact.document_id)
    chunk = next((c for c in document.chunks if c.id == fact.chunk_id), None) if document else None
    section = chunk.section_path if chunk else (document.label if document else "")
    return Evidence.from_fact(fact, section, chunk.page_to if chunk else None)


_SEVERITY_RANK = {Severity.RED: 0, Severity.YELLOW: 1}
_TERM_KEYS = frozenset(p.key for p in TERM_PATTERNS)  # сроки в днях: оплата, приёмка, поставка
_GOODS_FACTS = frozenset({"delivery_term_days"})  # есть только у поставки товара
_NO_GOODS = "не применимо: закупка услуг или работ — в контракте нет «Поставщика»"
# роль стороны в определениях контракта: «именуемое в дальнейшем «Исполнитель»»
_ROLE = re.compile(r"[«\"“]\s*(поставщик|исполнитель|подрядчик)\s*[»\"”]", re.IGNORECASE)


def supplies_goods(ctx: CheckContext) -> bool:
    """Закупка с поставкой товара. Нет — только если в контракте стороны названы «Исполнитель»
    или «Подрядчик» и ни разу «Поставщик» (пакеты ЕИС: уборка, ремонт). Контракта нет или роль
    не названа — считаем поставкой: «не найдено» тогда остаётся замечанием."""
    roles = {
        match.group(1).lower()
        for document in ctx.documents_of([DocType.CONTRACT])
        for chunk in document.chunks
        for match in _ROLE.finditer(chunk.text)
    }
    return not roles or "поставщик" in roles


def other_channel_fact(fact: FactValue) -> FactValue | None:
    """Значение второго канала карточки, если каналы разошлись (`merge_channels`)."""
    conflict = fact.conflict
    if not conflict or conflict.get("value") is None:
        return None
    return replace(
        fact,
        value=conflict["value"],
        unit=conflict.get("unit"),
        quote=str(conflict.get("quote") or ""),
        chunk_id=str(conflict.get("chunk_id") or ""),
        page_from=int(conflict.get("page") or fact.page_from),
        char_start=int(conflict.get("char_start") or 0),
        char_end=int(conflict.get("char_end") or 0),
        score=100.0,
        source="rules",
        conflict=None,
    )


def candidate_facts(fact: FactValue) -> list[FactValue]:
    """Другие значения поля в документе (этапы, расхождение цифр и прописи) — как факты."""
    result = []
    for item in fact.candidates:
        value = item.get("value")
        if value is None:
            continue
        if FACTS[fact.key].kind is FactKind.NUMBER and isinstance(value, int | float):
            value = float(value)
        result.append(
            replace(
                fact,
                value=value,
                unit=item.get("unit"),
                quote=str(item.get("quote") or ""),
                chunk_id=str(item.get("chunk_id") or ""),
                page_from=int(item.get("page") or fact.page_from),
                char_start=int(item.get("char_start") or 0),
                char_end=int(item.get("char_end") or 0),
                score=100.0,
                source="rules",
                conflict={"note": item["note"]} if item.get("note") else None,
                candidates=(),
            )
        )
    return result


def _first_condition(rule: ThresholdRule, fact: FactValue) -> ThresholdCondition | None:
    value, _converted = comparable(fact)
    return next((c for c in rule.conditions if parse_condition(c.when).evaluate(value)), None)


def _quoted(text: str) -> str:
    return "«" + " ".join(text.split()) + "»"


def _read_in_place(rule: ThresholdRule, term: UnreadableTerm, options: Sequence[FactValue]) -> bool:
    """Значение из `options` с цитатой в предложении неразобранного срока и тем же числом."""
    start = term.chunk.char_start + term.start
    end = term.chunk.char_start + term.end
    return any(
        o.document_id == term.chunk.document_id
        and o.char_start < end
        and start < o.char_end
        and isinstance(o.value, int | float)
        and not isinstance(o.value, bool)
        and o.value != 0  # «0» value_matches_quote пропускает без числа в тексте
        and value_matches_quote(FACTS[rule.fact], float(o.value), term.raw)
        for o in options
    )


def _other_terms(rule: ThresholdRule, ctx: CheckContext) -> list[FactValue]:
    """Прочитанные сроки других полей — как `_claimed` у грамматики: «оплата в течение 7 дней с
    даты подписания документа о приемке» — срок оплаты, а не неразобранный срок приёмки."""
    if rule.fact not in _TERM_KEYS:
        return []
    found: list[FactValue] = []
    for document in ctx.facts:
        for key, value in document.facts.items():
            if key in _TERM_KEYS and key != rule.fact:
                other = other_channel_fact(value)
                found += [value, *([other] if other else []), *candidate_facts(value)]
    return found


# единица срока → дней в ней; дни без указания вида толкуются и как рабочие, и как календарные
_TERM_UNITS = (
    (re.compile(r"час"), 1 / 24),
    (re.compile(r"недел"), 7.0),
    (re.compile(r"месяц"), 30.0),
    (re.compile(r"год|лет"), 365.0),
    (re.compile(r"дн|день|сут"), 1.0),
)


def _term_readings(key: str, raw: str) -> list[float]:
    """Все толкования неразобранного срока в единицах порога: число как есть и пересчёт
    календарных дней в рабочие (для полей «в рабочих днях») или обратно. Пусто — число или
    единицу не понять."""
    text = normalize(raw)
    found = numbers_in(text) or [float(n) for n in spelled_numbers(text)]
    days = next((size for unit, size in _TERM_UNITS if unit.search(text)), None)
    if not found or days is None:
        return []
    total = found[0] * days
    if key.endswith("_working_days"):
        return [total, total * WORKING_PER_CALENDAR]
    return [total, total / WORKING_PER_CALENDAR]


def _cannot_be_risk(rule: ThresholdRule, term: UnreadableTerm) -> bool:
    """Неразобранный срок не даёт риска ни при одном толковании («1 час», «3 рабочих дня» при
    пороге «больше 7») — вывод по полю он не меняет. Срок, который мог бы сработать, остаётся
    «проверьте вручную» (безопасность зелёного)."""
    readings = _term_readings(rule.fact, term.raw) if rule.fact in _TERM_KEYS else []
    return bool(readings) and not any(
        parse_condition(c.when).evaluate(value) for c in rule.conditions for value in readings
    )


def _same(a: FactValue, b: FactValue) -> bool:
    return a.value == b.value and (a.unit or "") == (b.unit or "")


@register("threshold")
class ThresholdEvaluator(RuleEvaluator):
    def evaluate(self, rules: Sequence[ThresholdRule], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        return [self._evaluate_one(rule, ctx) for rule in rules]

    def _evaluate_one(self, rule: ThresholdRule, ctx: CheckContext) -> RuleOutcomeDraft:
        fact = ctx.card_for(rule.applies_to).get(rule.fact)
        # значения поля, которые карточка не прочитала: испорченное число («4S») или срок в
        # незнакомой форме («по истечении 45 рабочих дней») — «в порядке» по ним нельзя
        documents = ctx.documents_of(rule.applies_to)
        broken = [t for d in documents for t in unreadable_terms(rule.fact, d.chunks)]
        unparsed = [t for d in documents for t in unparsed_terms(rule.fact, d.chunks)]
        elsewhere = _other_terms(rule, ctx)
        unparsed = [
            t
            for t in unparsed
            if not _read_in_place(rule, t, elsewhere) and not _cannot_be_risk(rule, t)
        ]
        if fact is None:
            unreadable = broken + unparsed
            if unreadable:
                return self._unreadable(rule, unreadable)
            if rule.fact in _GOODS_FACTS and not supplies_goods(ctx):
                # услуги или работы: срока поставки нет и быть не должно — не замечание
                return not_checked(rule.id, _NO_GOODS)
            return self._missing(rule, ctx)
        # Значений может быть несколько: второй канал карточки прочитал иначе, в документе
        # несколько сроков (этапы), цифры и пропись разошлись. Светофор — по худшему из них:
        # пропущенный риск обходится поставщику дороже, чем лишняя минута проверки
        other = other_channel_fact(fact)
        options = [fact, *([other] if other else []), *candidate_facts(fact)]
        # то же поле в других документах правила: приложение к контракту, вторая редакция —
        # карточка показывает одно значение, а порог проверяет все
        for document in ctx.facts:
            extra = document.facts.get(rule.fact)
            if document.doc_type in rule.applies_to and extra is not None and extra is not fact:
                options += [extra, *candidate_facts(extra)]
        # незнакомую грамматике форму могла прочитать модель — в том же месте и тем же числом
        # значение уже сравнивается с порогом; испорченное число («4S») не снимается никогда
        unreadable = broken + [t for t in unparsed if not _read_in_place(rule, t, options)]
        hits = [(option, _first_condition(rule, option)) for option in options]
        worst = min(
            ((o, h) for o, h in hits if h is not None),
            key=lambda pair: _SEVERITY_RANK[pair[1].severity],
            default=None,
        )
        if worst is None:
            if unreadable:  # прочитанные значения в норме, но одно число не прочитано
                return self._unreadable(rule, unreadable)
            return ok_with_evidence(rule.id, fact_evidence(fact, ctx))
        chosen, condition = worst
        others = [o for o in options if o is not chosen and not _same(o, chosen)]
        return self._violation(rule, ctx, chosen, condition, fact, other, others)

    @staticmethod
    def _notes(
        rule: ThresholdRule,
        chosen: FactValue,
        primary: FactValue,
        model_other: FactValue | None,
        others: list[FactValue],
    ) -> list[str]:
        notes: list[str] = []
        if (chosen.conflict or {}).get("note") == "digits_words":
            notes.append(
                f"Цифры и пропись в документе расходятся ({_quoted(chosen.quote)}) — возможна "
                "опечатка или ошибка распознавания. Светофор — по худшему значению."
            )
        if model_other is not None and model_other is chosen:
            notes.append(
                f"Модель прочитала другое значение — {display_value(rule.fact, primary.value)} "
                f"({_quoted(primary.quote)}). Светофор показан по худшему из двух значений — "
                "проверьте по документу."
            )
        elif model_other is not None and model_other in others:
            notes.append(
                f"Второй способ проверки нашёл другое значение — "
                f"{display_value(rule.fact, model_other.value)} ({_quoted(model_other.quote)}). "
                "Проверьте по документу."
            )
        rest = [o for o in others if o is not model_other]
        if rest:
            listed = "; ".join(
                f"{display_value(rule.fact, o.value)} ({_quoted(o.quote)})" for o in rest[:3]
            )
            notes.append(f"В документе есть и другие значения: {listed}.")
        return notes

    def _violation(
        self,
        rule: ThresholdRule,
        ctx: CheckContext,
        chosen: FactValue,
        condition: ThresholdCondition,
        primary: FactValue,
        model_other: FactValue | None,
        others: list[FactValue],
    ) -> RuleOutcomeDraft:
        value, converted = comparable(chosen)
        shown = display_value(rule.fact, value if converted else chosen.value)
        notes = []
        if converted:
            shown = f"≈{shown}"
            notes.append(
                f"В документе срок указан в календарных днях "
                f"({display_value(rule.fact, chosen.value)}); для сравнения с порогом в рабочих "
                "днях он пересчитан приближённо (×5/7)."
            )
        notes.extend(self._notes(rule, chosen, primary, model_other, others))
        evidence = [fact_evidence(chosen, ctx)]
        evidence += [fact_evidence(o, ctx) for o in others[:2] if o.quote]
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.VIOLATION,
            severity=condition.severity,
            title=rule.title,
            message=render(rule.message, value=shown, quote=chosen.quote),
            explanation=" ".join(notes) or None,
            evidence=tuple(evidence),
            document_id=chosen.document_id,
            related={
                "fact": rule.fact,
                "value": chosen.value,
                "unit": chosen.unit,
                "channels_disagree": model_other is not None,
                "other_values": [o.value for o in others],
            },
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, (finding,))

    @staticmethod
    def _unreadable(rule: ThresholdRule, terms: list[UnreadableTerm]) -> RuleOutcomeDraft:
        """Значение поля есть, но не прочитано: испорченное число («4S», «4 5») или незнакомая
        форма («по истечении 45 рабочих дней», «два месяца»). «В порядке» тогда нельзя.

        Есть «значение не найдено» у правила — его замечание с цитатой нечитаемого места, иначе
        «не проверено» с пометкой «нечитаемо» (критичный пробел, если правило может дать
        «критично»)."""
        places = "; ".join(
            f"«{' '.join(t.chunk.text[t.start : t.end].split())}» (стр. {t.chunk.page_from})"
            for t in terms[:2]
        )
        note = (
            f"Значение записано так, что система не смогла его прочитать "
            f"({', '.join(t.raw for t in terms[:3])}) — ошибка распознавания или необычная "
            f"формулировка: {places}. Проверьте по документу."
        )
        if rule.on_missing is None:
            return RuleOutcomeDraft(
                rule.id,
                RuleOutcome.NOT_CHECKED,
                f"значение записано так, что его не прочитать: {places}",
                details={"blocked": "unreadable", "unreadable_numbers": [t.raw for t in terms]},
            )
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.ABSENT,
            severity=rule.on_missing.severity,
            title=rule.title,
            message=render(rule.on_missing.message, value="—", quote=""),
            explanation=note,
            evidence=tuple(Evidence.from_chunk(t.chunk, t.start, t.end) for t in terms[:2]),
            document_id=terms[0].chunk.document_id,
            related={"fact": rule.fact, "unreadable_numbers": [t.raw for t in terms]},
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.ABSENT, None, (finding,))

    @staticmethod
    def _missing(rule: ThresholdRule, ctx: CheckContext) -> RuleOutcomeDraft:
        errors = ctx.facts_errors(rule.applies_to)
        if errors:
            return not_checked(rule.id, f"карточка закупки не заполнена: {errors[0]}", "facts")
        if rule.on_missing is None:
            return not_checked(
                rule.id, f"в документах не найдено: {FACTS[rule.fact].label.lower()}"
            )
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.ABSENT,
            severity=rule.on_missing.severity,
            title=rule.title,
            message=render(rule.on_missing.message, value="—", quote=""),
            related={
                "fact": rule.fact,
                "checked_documents": [d.label for d in ctx.documents_of(rule.applies_to)],
            },
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.ABSENT, None, (finding,))
