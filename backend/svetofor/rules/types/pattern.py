"""`pattern` — детерминированный поиск формулировок.

Без модели. Выражения проверяются по исходному тексту и по нормализованному (ё→е, единые
кавычки, тире и пробелы, склеенные переносы) — методологу не нужно помнить про «ё» и
переносы строк. Цитата — предложение с найденной формулировкой, точно из документа.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from svetofor.db.enums import FindingKind, RuleOutcome
from svetofor.rules.facts import SourceChunk
from svetofor.rules.model import PatternRule
from svetofor.rules.results import (
    CheckContext,
    DocumentView,
    Evidence,
    FindingDraft,
    RuleOutcomeDraft,
    not_checked,
    ok_by_absence,
)
from svetofor.rules.rule_regex import (
    RuleRegex,
    RuleRegexTimeoutError,
    TimeBudget,
    compile_rule_pattern,
)
from svetofor.rules.templates import render
from svetofor.rules.types import RuleEvaluator, register
from svetofor.rules.verify import NormalizedText, normalize_with_map

_SENTENCE_END = re.compile(r"[.!?;](?=\s)")
_MAX_QUOTE = 400
_CLAUSE_START = re.compile(r"\n(?=[ \t]*\d{1,2}(?:\.\d{1,3}){0,4}\.?[ \t]+[А-ЯЁA-Z«])")
_CLAUSE_LOOKAHEAD = 40  # длина «\n  1.2.3.4.5. О» с запасом
# точка после них — не конец предложения: «г. Москва», «ст. 34», «1 000 руб. в день»
_ABBREVIATIONS = frozenset(
    {
        "г",
        "гг",
        "ст",
        "п",
        "пп",
        "ч",
        "т",
        "д",
        "ул",
        "руб",
        "коп",
        "тыс",
        "млн",
        "млрд",
        "др",
        "см",
        "н",
        "ред",
        "им",
        "е",
        "о",
        "с",
        "р",
    }
)


def _is_sentence_end(text: str, index: int) -> bool:
    if text[index] != ".":
        return True
    word_start = index
    while word_start > 0 and text[word_start - 1].isalnum():
        word_start -= 1
    word = text[word_start:index]
    return not (word.isdigit() or word.lower() in _ABBREVIATIONS)  # «4.1.» — номер пункта


def _sentence_ends(text: str, start: int, end: int) -> list[int]:
    return [
        m.end()
        for m in _SENTENCE_END.finditer(text, start, end)
        if _is_sentence_end(text, m.start())
    ]


def sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Предложение вокруг [start, end).

    Перевод строки — не граница (в PDF строки рвут фразы), кроме начала нумерованного пункта:
    заголовок раздела без точки в конце не должен прилипать к цитате.
    """
    before = _sentence_ends(text, 0, start)
    left = before[-1] if before else 0
    # Заглядывание вперёд в _CLAUSE_START смотрит дальше номера пункта, поэтому строку не
    # обрезаем по границе поиска: совпадение может начинаться сразу за «2.1. ».
    clause_starts = [
        m.start() + 1
        for m in _CLAUSE_START.finditer(text, left, min(len(text), start + _CLAUSE_LOOKAHEAD))
        if m.start() < start
    ]
    left = clause_starts[-1] if clause_starts else left
    after = _sentence_ends(text, end, len(text))
    right = after[0] if after else len(text)
    next_clause = _CLAUSE_START.search(text, end, min(len(text), right + _CLAUSE_LOOKAHEAD))
    right = next_clause.start() if next_clause and next_clause.start() < right else right
    if right - left > _MAX_QUOTE:
        left = max(left, start - 150)
        right = min(right, end + 150)
        while left > 0 and not text[left - 1].isspace():
            left -= 1
        while right < len(text) and not text[right].isspace():
            right += 1
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    return left, right


def sentence_bounds(text: str) -> list[int]:
    """Границы предложений текста по тем же правилам, что у `sentence_span`: конец предложения
    (не «ст.», не «4.1.») и начало нумерованного пункта с новой строки. С 0 и len(text)."""
    clause_starts = [m.start() + 1 for m in _CLAUSE_START.finditer(text)]
    return sorted({0, len(text), *_sentence_ends(text, 0, len(text)), *clause_starts})


class ChunkText:
    def __init__(self, chunk: SourceChunk) -> None:
        self.chunk = chunk
        self._normalized: NormalizedText | None = None

    @property
    def normalized(self) -> NormalizedText:
        if self._normalized is None:
            self._normalized = normalize_with_map(self.chunk.text)
        return self._normalized

    def find(self, pattern: RuleRegex, budget: TimeBudget) -> tuple[int, int] | None:
        """Первое совпадение: смещения в исходном тексте фрагмента."""
        span = pattern.search(self.chunk.text, budget)
        if span is not None:
            return span
        norm = self.normalized
        span = pattern.search(norm.text, budget)
        return norm.original_span(*span) if span is not None else None


@register("pattern")
class PatternEvaluator(RuleEvaluator):
    def evaluate(self, rules: Sequence[PatternRule], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        return [self._evaluate_one(rule, ctx) for rule in rules]

    def _evaluate_one(self, rule: PatternRule, ctx: CheckContext) -> RuleOutcomeDraft:
        documents = ctx.documents_of(rule.applies_to)
        if not documents:
            return not_checked(rule.id, "в пакете нет документов, к которым относится правило")
        try:
            findings = self._findings(rule, documents, TimeBudget())
        except RuleRegexTimeoutError as exc:
            # выражение перебирает варианты дольше лимита: правило не проверено — это сбой,
            # а не «в порядке» (иначе зависшее правило давало бы зелёный)
            return not_checked(
                rule.id,
                f"выражение «{exc}» работает слишком долго на тексте документа — упростите его",
                "regex_timeout",
            )
        if findings:
            return RuleOutcomeDraft(rule.id, RuleOutcome.VIOLATION, None, tuple(findings))
        return ok_by_absence(rule.id)

    def _findings(
        self, rule: PatternRule, documents: Sequence[DocumentView], budget: TimeBudget
    ) -> list[FindingDraft]:
        patterns = [compile_rule_pattern(p) for p in rule.patterns]
        excludes = [compile_rule_pattern(p) for p in rule.exclude_patterns]
        findings: list[FindingDraft] = []
        for document in documents:
            texts = [ChunkText(c) for c in document.chunks]
            if rule.scope == "document" and any(
                t.find(ex, budget) for t in texts for ex in excludes
            ):
                continue
            hit = self._first_hit(
                texts, patterns, excludes if rule.scope == "chunk" else [], budget
            )
            if hit is None:
                continue
            chunk, (start, end) = hit
            left, right = sentence_span(chunk.text, start, end)
            evidence = Evidence.from_chunk(chunk, left, right)
            findings.append(
                FindingDraft(
                    rule_id=rule.id,
                    kind=FindingKind.VIOLATION,
                    severity=rule.severity,
                    title=rule.title,
                    message=render(rule.message, quote=evidence.quote),
                    evidence=(evidence,),
                    document_id=document.id,
                )
            )
        return findings

    @staticmethod
    def _first_hit(
        texts: list[ChunkText],
        patterns: list[RuleRegex],
        chunk_excludes: list[RuleRegex],
        budget: TimeBudget,
    ) -> tuple[SourceChunk, tuple[int, int]] | None:
        for text in texts:
            if any(text.find(ex, budget) for ex in chunk_excludes):
                continue
            for pattern in patterns:
                span = text.find(pattern, budget)
                if span is not None:
                    return text.chunk, span
        return None
