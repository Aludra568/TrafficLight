"""`internal_consistency` — одно условие указано в документе по-разному.

Пример организаторов: «закупаем 5000 яблок» в одном пункте и «3000 яблок» в другом. Грамматика
карточки собирает все значения поля по документу (`candidates`), здесь они сверяются между
собой. Законные «другие значения» не считаются противоречием:
- часть общего количества — партия, этап, позиция, «в том числе»;
- предел или диапазон из закона — «не более 30 %», «от 0,5 до 30 %»;
- список позиций — у поля три и больше разных значения;
- другая единица — «5000 пачек (500 коробок)».
«В порядке» — по отсутствию второго значения во всём тексте: при нечитаемых страницах движок
заменит его на «не проверено» (`engine.guard`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict

from svetofor.db.enums import FindingKind, RuleOutcome
from svetofor.rules.facts import FactValue
from svetofor.rules.facts_catalog import FACTS
from svetofor.rules.model import InternalConsistencyRule
from svetofor.rules.results import (
    CheckContext,
    FindingDraft,
    RuleOutcomeDraft,
    not_checked,
    ok_by_absence,
)
from svetofor.rules.templates import render
from svetofor.rules.types import RuleEvaluator, register
from svetofor.rules.types.cross_document import same_value, shown
from svetofor.rules.types.threshold import candidate_facts, fact_evidence
from svetofor.rules.verify import normalize

# часть общего значения, предел или диапазон — не второе значение того же условия
_NOT_THE_SAME = re.compile(
    r"парти|этап|в том числе|из них|позици|\bлот|ежемесячн|ежекварт|в месяц|в квартал|в год"
    r"|не более|не менее|не может|не превыша|не должн|превышающ|максимальн\w* размер"
    r"|минимальн\w* размер|\bот \d[\d ,.]*(?:%|процент\w*)? до\b|\bдо \d"
)
_MAX_VALUES = 2  # больше разных значений — список позиций или этапов, а не противоречие


def _unit_key(fact: FactValue) -> str:
    """«пачек» и «пачки», «шт.» и «штук» — одна единица."""
    return (fact.unit or "").lower()[:2]


def _comparable(fact: FactValue) -> bool:
    return not _NOT_THE_SAME.search(normalize(fact.quote))


def contradiction(fact: FactValue) -> FactValue | None:
    """Другое значение того же поля, если документ ему противоречит; иначе None."""
    if not _comparable(fact):
        return None
    others = [
        c for c in candidate_facts(fact) if _unit_key(c) == _unit_key(fact) and _comparable(c)
    ]
    differing = [c for c in others if not same_value(fact, c)]
    values = {f.value for f in (fact, *differing)}
    if not differing or len(values) > _MAX_VALUES:
        return None
    return differing[0]


@register("internal_consistency")
class InternalConsistencyEvaluator(RuleEvaluator):
    def evaluate(
        self, rules: Sequence[InternalConsistencyRule], ctx: CheckContext
    ) -> list[RuleOutcomeDraft]:
        return [self._evaluate_one(rule, ctx) for rule in rules]

    def _evaluate_one(self, rule: InternalConsistencyRule, ctx: CheckContext) -> RuleOutcomeDraft:
        found: list[FactValue] = []
        findings: list[FindingDraft] = []
        for document_facts in ctx.facts:
            if document_facts.doc_type not in rule.applies_to:
                continue
            for key in rule.facts:
                fact = document_facts.facts.get(key)
                if fact is None:
                    continue
                found.append(fact)
                other = contradiction(fact)
                if other is not None:
                    findings.append(_finding(rule, ctx, fact, other))
        if findings:
            return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, tuple(findings))
        if found:
            evidence = [asdict(fact_evidence(f, ctx)) for f in found]
            return ok_by_absence(rule.id, evidence=evidence)
        errors = ctx.facts_errors(rule.applies_to)
        if errors:
            return not_checked(rule.id, f"карточка закупки не заполнена: {errors[0]}", "facts")
        return not_checked(rule.id, "в документах не найдено ни одного значения для сверки")


def _finding(
    rule: InternalConsistencyRule, ctx: CheckContext, first: FactValue, second: FactValue
) -> FindingDraft:
    document = ctx.document(first.document_id)
    return FindingDraft(
        rule_id=rule.id,
        kind=FindingKind.CONTRADICTION,
        severity=rule.severity,
        title=rule.title,
        message=render(
            rule.message,
            field=FACTS[first.key].label.lower(),
            first=shown(first),
            second=shown(second),
            document=document.label if document else "",
        ),
        evidence=(fact_evidence(first, ctx), fact_evidence(second, ctx)),
        related={"fact": first.key, "values": [first.value, second.value]},
    )
