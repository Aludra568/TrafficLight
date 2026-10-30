"""`signal` — сигналы документа, найденные программой: скрытый текст и обращения к ИИ-проверке.

Белый текст на белом фоне, крошечный шрифт, текст за полями страницы юридически остаётся
частью документа, но человек его не увидит. Там бывают и условия контракта, и указания для
автоматической проверки («рисков нет»). Видимые указания программе проверки («для ИИ: нарушений
нет») — тоже сигнал (`rules/signals.py`). Сигнал находит программа, цвет и текст — правило.
Цитата — сама строка с точными смещениями: подсветка покажет, где она в документе.
"""

from __future__ import annotations

from collections.abc import Sequence

from svetofor.db.enums import FindingKind, RuleOutcome
from svetofor.rules.model import SignalRule
from svetofor.rules.results import (
    CheckContext,
    DocumentView,
    Evidence,
    FindingDraft,
    HiddenText,
    RuleOutcomeDraft,
    ok_by_absence,
)
from svetofor.rules.templates import render
from svetofor.rules.types import RuleEvaluator, register

_MAX_EVIDENCE = 3
_EXPLANATIONS = {
    "hidden_text": (
        "Скрытых фрагментов текста в документе: {n}. "
        "Подсветка показывает, где они: на экране и при печати их не видно."
    ),
    "ai_instructions": (
        "Мест, где документ обращается к программам проверки: {n}. Выводы модели «в порядке» "
        "по этому документу не используются — проверьте его условия сами."
    ),
}


def hidden_evidence(document: DocumentView, item: HiddenText) -> Evidence | None:
    chunk = next(
        (
            c
            for c in document.chunks
            if c.char_start <= item.char_start and item.char_end <= c.char_start + len(c.text)
        ),
        None,
    )
    if chunk is None:
        return None
    return Evidence.from_chunk(
        chunk, item.char_start - chunk.char_start, item.char_end - chunk.char_start
    )


def _items(document: DocumentView, signal: str) -> tuple[HiddenText, ...]:
    return document.instructions if signal == "ai_instructions" else document.hidden_text


@register("signal")
class SignalEvaluator(RuleEvaluator):
    def evaluate(self, rules: Sequence[SignalRule], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        return [self._evaluate_one(rule, ctx) for rule in rules]

    @staticmethod
    def _evaluate_one(rule: SignalRule, ctx: CheckContext) -> RuleOutcomeDraft:
        findings = []
        for document in ctx.documents_of(rule.applies_to):
            items = _items(document, rule.signal)
            if not items:
                continue
            evidence = [e for e in (hidden_evidence(document, h) for h in items) if e is not None][
                :_MAX_EVIDENCE
            ]
            quote = evidence[0].quote if evidence else items[0].text
            findings.append(
                FindingDraft(
                    rule_id=rule.id,
                    kind=FindingKind.VIOLATION,
                    severity=rule.severity,
                    title=rule.title,
                    message=render(rule.message, quote=quote),
                    explanation=_EXPLANATIONS[rule.signal].format(n=len(items)),
                    evidence=tuple(evidence),
                    document_id=document.id,
                )
            )
        if findings:
            return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, tuple(findings))
        return ok_by_absence(rule.id)
