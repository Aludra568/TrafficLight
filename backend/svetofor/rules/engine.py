"""Движок правил: каждому включённому правилу — ровно один исход.

Правило, которое нельзя применить (нет нужного документа, другая процедура закупки), получает
«не проверено» с понятной причиной — оно не исчезает из отчёта молча. Тип правила находится
по реестру `rules/types`, поэтому новый тип не требует правок движка.

После исполнения исходы проходят защиту от ложного зелёного:
«не нашли» доказывает отсутствие риска, только если текст документов прочитан полностью, а
«в порядке» от модели — только если документ не пытается давать указания программе проверки.
"""

from __future__ import annotations

from collections.abc import Sequence

from svetofor.db.enums import FindingStatus, RuleOutcome
from svetofor.rules.facts_catalog import display_value
from svetofor.rules.model import LlmCheckRule, Rule
from svetofor.rules.results import (
    BASIS_EVIDENCE,
    CheckContext,
    RuleOutcomeDraft,
    basis_of,
    doc_types_text,
    not_checked,
)
from svetofor.rules.rule_regex import RuleRegexTimeoutError, TimeBudget, compile_rule_pattern
from svetofor.rules.types import RULE_TYPES, EngineDeps, RuleEvaluator, load_builtin_types
from svetofor.rules.types.pattern import ChunkText, sentence_span


def red_flag(rule: LlmCheckRule, ctx: CheckContext) -> str | None:
    """Тревожная формулировка правила в его документах: «…» (стр. N) или None.

    Не уложились во время — считаем, что формулировка может быть: молчание не доказательство.
    """
    if not rule.red_flags:
        return None
    flags = [compile_rule_pattern(p) for p in rule.red_flags]
    budget = TimeBudget()
    try:
        for document in ctx.documents_of(rule.applies_to):
            for piece in document.chunks:
                text = ChunkText(piece)
                for flag in flags:
                    span = text.find(flag, budget)
                    if span is not None:
                        left, right = sentence_span(piece.text, *span)
                        quote = " ".join(piece.text[left:right].split())
                        return f"«{quote}» (стр. {piece.page_from})"
    except RuleRegexTimeoutError:
        return "(поиск тревожных формулировок не уложился во время)"
    return None


def _other_law(rule: Rule, ctx: CheckContext) -> bool:
    return bool(ctx.law) and ctx.law not in rule.law


def inapplicable_reason(rule: Rule, ctx: CheckContext) -> str | None:
    if _other_law(rule, ctx):
        procedure = display_value("law", ctx.law)
        return f"правило для {', '.join(rule.law)}, а закупка — {procedure}"
    if not ctx.documents_of(rule.applies_to):
        return f"в пакете нет документа: {doc_types_text(rule.applies_to)}"
    return None


def _blocked(outcome: RuleOutcomeDraft, reason: str, kind: str) -> RuleOutcomeDraft:
    """«Не проверено» вместо недоказанного исхода. Отклонённые замечания сохраняются для учёта."""
    kept = tuple(f for f in outcome.findings if f.status is FindingStatus.REJECTED_UNVERIFIED)
    details = {**(outcome.details or {}), "blocked": kind, "was": outcome.outcome.value}
    return RuleOutcomeDraft(outcome.rule_id, RuleOutcome.NOT_CHECKED, reason, kept, details)


def guard(rule: Rule, outcome: RuleOutcomeDraft, ctx: CheckContext) -> RuleOutcomeDraft:
    """Не дать «в порядке» и «условие не найдено» там, где система не могла это проверить."""
    if outcome.outcome not in (RuleOutcome.OK, RuleOutcome.ABSENT):
        return outcome  # найденный риск остаётся риском
    documents = ctx.documents_of(rule.applies_to)
    if isinstance(rule, LlmCheckRule):
        steered = next((d for d in documents if d.instructions), None)
        if steered is not None:
            return _blocked(
                outcome,
                f"в документе «{steered.label}» есть текст, обращённый к программам проверки — "
                "вывод модели по нему не используется",
                "instructions",
            )
    if isinstance(rule, LlmCheckRule) and outcome.outcome is RuleOutcome.OK:
        flagged = red_flag(rule, ctx)
        if flagged is not None:
            # модель сочла условие нормальным или не нашла его, а в тексте явная тревожная
            # формулировка: «простить» риск может только человек
            return _blocked(
                outcome,
                f"в документе есть формулировка {flagged}, а модель сочла условие нормальным — "
                "проверьте вручную",
                "red_flag",
            )
        details = outcome.details or {}
        if details.get("whole_text") is False and not (
            ctx.semantic_search and details.get("semantic", True)
        ):
            # документ не поместился целиком, а фрагменты подобраны только по словам: цитата
            # «в порядке» доказывает норму в одном пункте, но не отсутствие рискованного пункта
            # в другом («вправе отказаться … по любым основаниям»)
            return _blocked(
                outcome,
                "документ не поместился целиком, а смысловой поиск не работал — фрагменты "
                "подобраны по словам, «в порядке» по части текста не доказано",
                "partial_search",
            )
    if outcome.outcome is RuleOutcome.OK and basis_of(outcome) == BASIS_EVIDENCE:
        return outcome  # значение найдено и проверено цитатой
    unreadable = next((d for d in documents if d.unreadable), None)
    if unreadable is not None:
        return _blocked(
            outcome,
            f"{unreadable.unreadable_reason()} — отсутствие риска не подтверждено",
            "unreadable",
        )
    return outcome


class RuleEngine:
    def __init__(self, deps: EngineDeps) -> None:
        load_builtin_types()
        self.deps = deps
        self._evaluators = {name: cls(deps) for name, cls in RULE_TYPES.items()}

    def evaluator(self, type_name: str) -> RuleEvaluator:
        return self._evaluators[type_name]

    def run(self, rules: Sequence[Rule], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        enabled = [rule for rule in rules if rule.enabled]
        outcomes: dict[str, RuleOutcomeDraft] = {}
        by_type: dict[str, list[Rule]] = {}
        for rule in enabled:
            reason = inapplicable_reason(rule, ctx)
            if reason is not None:
                outcomes[rule.id] = not_checked(rule.id, reason)
                if _other_law(rule, ctx):
                    # правило другого закона к этой закупке не относится: в полноту проверки
                    # не входит, иначе пакет 223-ФЗ всегда «не оценён» из-за правил 44-ФЗ
                    outcomes[rule.id] = outcomes[rule.id].with_details(other_law=True)
                elif not ctx.documents_of(rule.applies_to):
                    # документа нет вовсе: проверка не выполнена, а не «условия нет» —
                    # для правила «критично» это пробел, зелёный невозможен
                    outcomes[rule.id] = outcomes[rule.id].with_details(
                        missing_documents=[t.value for t in rule.applies_to]
                    )
            else:
                by_type.setdefault(rule.type, []).append(rule)
        for type_name, typed in by_type.items():
            evaluator = self._evaluators.get(type_name)
            if evaluator is None:
                for rule in typed:
                    outcomes[rule.id] = not_checked(
                        rule.id, f"тип правила «{type_name}» не поддерживается"
                    )
                continue
            by_id = {rule.id: rule for rule in typed}
            for outcome in evaluator.evaluate(typed, ctx):
                outcomes[outcome.rule_id] = guard(by_id[outcome.rule_id], outcome, ctx)
        return [outcomes[rule.id] for rule in enabled]
