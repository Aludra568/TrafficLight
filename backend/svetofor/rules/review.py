"""Второе мнение по красным нарушениям, найденным моделью.

Модель, нашедшая нарушение, «защищает» свой вывод; независимая перепроверка с другой установкой
(«найди ошибку в чужом решении») ловит цитату не о том, оговорку рядом, вывод по другому пункту.
Рецензент не скрывает и не перекрашивает замечание — цвет задаёт правило, — а при несогласии
помечает его «требует проверки специалистом» со своим доводом. Сбой рецензента — замечание как было.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from svetofor.db.enums import FindingKind, Severity
from svetofor.llm.errors import LLMError
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import render
from svetofor.llm.schemas import ReviewResult
from svetofor.rules.model import LlmCheckRule
from svetofor.rules.results import CheckContext, FindingDraft, RuleOutcomeDraft

REVIEW_TASK = "review"
REVIEW_TEMPLATE = "review.v1"
_PER_CALL = 6
_TOKENS_PER_ITEM = 200


@dataclass(frozen=True)
class _Item:
    id: str
    rule: LlmCheckRule
    quote: str
    section_path: str
    page: int
    context: str
    outcome_index: int
    finding_index: int


def _items(
    outcomes: Sequence[RuleOutcomeDraft], rules: dict[str, LlmCheckRule], ctx: CheckContext
) -> list[_Item]:
    items: list[_Item] = []
    for i, outcome in enumerate(outcomes):
        rule = rules.get(outcome.rule_id)
        if rule is None:
            continue
        for j, finding in enumerate(outcome.findings):
            if (
                finding.kind is not FindingKind.VIOLATION
                or finding.severity is not Severity.RED
                or not finding.evidence
            ):
                continue
            evidence = finding.evidence[0]
            document = ctx.document(evidence.document_id)
            chunk = next(
                (c for c in (document.chunks if document else ()) if c.id == evidence.chunk_id),
                None,
            )
            items.append(
                _Item(
                    id=f"R{len(items) + 1}",
                    rule=rule,
                    quote=evidence.quote,
                    section_path=evidence.section_path,
                    page=evidence.page_from,
                    context=chunk.text if chunk else evidence.quote,
                    outcome_index=i,
                    finding_index=j,
                )
            )
    return items


def second_opinion(
    outcomes: list[RuleOutcomeDraft],
    rules: Sequence[LlmCheckRule],
    ctx: CheckContext,
    gateway: LLMGateway,
    check_id: uuid.UUID | None = None,
) -> list[RuleOutcomeDraft]:
    """Перепроверить красные нарушения модели. Возвращает исходы с отметками рецензента."""
    by_id = {rule.id: rule for rule in rules}
    items = _items(outcomes, by_id, ctx)
    result = list(outcomes)
    for start in range(0, len(items), _PER_CALL):
        batch = items[start : start + _PER_CALL]
        prompt = render(REVIEW_TEMPLATE, items=batch)
        try:
            answer = gateway.complete_json(
                task=REVIEW_TASK,
                model_role="main",
                system=prompt.system,
                user=prompt.user,
                schema=ReviewResult,
                prompt_version=prompt.version,
                max_tokens=_TOKENS_PER_ITEM * len(batch) + 100,
                check_id=check_id,
            )
        except LLMError:
            continue  # второе мнение — дополнительная проверка, без него замечание остаётся
        decisions = {r.item_id.strip(): r for r in answer.value.reviews}
        for item in batch:
            review = decisions.get(item.id)
            if review is None:
                continue
            outcome = result[item.outcome_index]
            findings = list(outcome.findings)
            findings[item.finding_index] = _marked(
                findings[item.finding_index], review.decision, review.reasoning
            )
            result[item.outcome_index] = replace(outcome, findings=tuple(findings))
    return result


def _marked(finding: FindingDraft, decision: str, reasoning: str) -> FindingDraft:
    review = {"decision": decision, "comment": " ".join(reasoning.split())[:400] or None}
    related = {**(finding.related or {}), "review": review}
    if decision != "confirmed":
        related["needs_review"] = True
    return replace(finding, related=related)
