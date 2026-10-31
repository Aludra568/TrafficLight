"""`cross_document` — противоречия между документами.

Сравниваются факты, извлечённые отдельно по каждому документу. Сроки с разными единицами
(«10 календарных» и «10 рабочих») — тоже противоречие: это разные сроки. Цитаты — из обоих
документов, чтобы специалист сразу видел, что с чем не сходится.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from svetofor.db.enums import DocType, FindingKind, RuleOutcome
from svetofor.rules.facts import FactValue
from svetofor.rules.facts_catalog import FACTS, FactKind, display_value
from svetofor.rules.model import CrossDocumentRule
from svetofor.rules.results import (
    CheckContext,
    FindingDraft,
    RuleOutcomeDraft,
    doc_types_text,
    not_checked,
    ok_with_evidence,
)
from svetofor.rules.templates import render
from svetofor.rules.types import RuleEvaluator, register
from svetofor.rules.types.threshold import fact_evidence
from svetofor.rules.verify import normalize

_UNIT_TITLES = {"working": "раб. дн.", "calendar": "календ. дн."}


def normalize_unit(unit: str | None) -> str:
    """Нормализация единицы измерения: синонимы к каноническому виду, нижний регистр."""
    if not unit:
        return ""
    cleaned = re.sub(r"[\s.]+", "", unit.lower())
    if not cleaned:
        return ""
    if cleaned in {"шт", "ед"} or cleaned.startswith("штук") or cleaned.startswith("единиц"):
        return "шт"
    if (
        cleaned in {"м2", "м²", "квм", "квм2", "квм²"}
        or cleaned.startswith("квметр")
        or cleaned.startswith("квадратн")
    ):
        return "м2"
    if (
        cleaned in {"м3", "м³", "кубм", "кубм3", "кубм³"}
        or cleaned.startswith("кубметр")
        or cleaned.startswith("кубометр")
        or cleaned.startswith("кубическ")
    ):
        return "м3"
    if cleaned == "кг" or cleaned.startswith("килограмм"):
        return "кг"
    if cleaned == "т" or cleaned.startswith("тонн"):
        return "т"
    if cleaned == "л" or cleaned.startswith("литр"):
        return "л"
    if (
        cleaned in {"м", "погм", "пм"}
        or cleaned.startswith("погметр")
        or cleaned.startswith("погонн")
        or cleaned.startswith("метр")
    ):
        return "м"
    return cleaned


def best_fact(ctx: CheckContext, key: str, doc_type: DocType) -> FactValue | None:
    candidates = [f.facts[key] for f in ctx.facts if f.doc_type is doc_type and key in f.facts]
    return max(candidates, key=lambda f: f.score) if candidates else None


def same_value(a: FactValue, b: FactValue) -> bool:
    kind = FACTS[a.key].kind
    if kind is FactKind.NUMBER and isinstance(a.value, float) and isinstance(b.value, float):
        if a.key.endswith("_days") and a.unit and b.unit and a.unit != b.unit:
            return False
        return abs(a.value - b.value) <= 1e-6 * max(1.0, abs(a.value))
    if isinstance(a.value, str) and isinstance(b.value, str):
        return normalize(a.value) == normalize(b.value)
    return a.value == b.value


def shown(fact: FactValue) -> str:
    text = display_value(fact.key, fact.value)
    unit = _UNIT_TITLES.get(fact.unit or "") if fact.key.endswith("_days") else fact.unit
    return f"{text} {unit}" if unit else text


@register("cross_document")
class CrossDocumentEvaluator(RuleEvaluator):
    def evaluate(
        self, rules: Sequence[CrossDocumentRule], ctx: CheckContext
    ) -> list[RuleOutcomeDraft]:
        return [self._evaluate_one(rule, ctx) for rule in rules]

    def _evaluate_one(self, rule: CrossDocumentRule, ctx: CheckContext) -> RuleOutcomeDraft:
        first_type, second_type = rule.compare
        missing = [t for t in rule.compare if not ctx.documents_of([t])]
        if missing:
            return not_checked(rule.id, f"для сравнения нужен документ: {doc_types_text(missing)}")
        first = best_fact(ctx, rule.fact, first_type)
        second = best_fact(ctx, rule.fact, second_type)
        if first is None or second is None:
            errors = ctx.facts_errors(rule.compare)
            if errors:
                return not_checked(rule.id, f"карточка закупки не заполнена: {errors[0]}", "facts")
            label = FACTS[rule.fact].label.lower()
            return not_checked(rule.id, f"значение «{label}» найдено не в обоих документах")
        spec = FACTS[rule.fact]
        if (
            spec.kind is FactKind.NUMBER
            and not rule.fact.endswith("_days")
            and first.unit
            and second.unit
            and first.unit.strip()
            and second.unit.strip()
            and normalize_unit(first.unit) != normalize_unit(second.unit)
        ):
            return not_checked(
                rule.id,
                f"В документах разные единицы измерения: {first.unit.strip()} и "
                f"{second.unit.strip()} — сравнить нельзя",
            )
        if same_value(first, second):
            return ok_with_evidence(rule.id, fact_evidence(first, ctx), fact_evidence(second, ctx))
        finding = FindingDraft(
            rule_id=rule.id,
            kind=FindingKind.CONTRADICTION,
            severity=rule.severity,
            title=rule.title,
            message=render(
                rule.message, **{first_type.value: shown(first), second_type.value: shown(second)}
            ),
            evidence=(fact_evidence(first, ctx), fact_evidence(second, ctx)),
            related={
                "fact": rule.fact,
                "values": {first_type.value: first.value, second_type.value: second.value},
                "units": {first_type.value: first.unit, second_type.value: second.unit},
            },
        )
        return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, (finding,))
