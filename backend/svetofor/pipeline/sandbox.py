"""Песочница правила: правило на вставленном тексте.

Тот же движок, та же проверка цитат, что и в настоящей проверке, — методолог видит ровно то,
что увидит специалист. Правило может быть ещё не сохранённым черновиком.
"""

from __future__ import annotations

import re
from typing import Any

from svetofor.db.enums import DocType, FindingStatus, RuleOutcome
from svetofor.pipeline.orchestrator import AnalysisDeps
from svetofor.rules.engine import RuleEngine
from svetofor.rules.fact_rules import extract_rule_facts, merge_channels
from svetofor.rules.facts import DocumentFacts, FactExtractor, SourceChunk, facts_for
from svetofor.rules.facts_catalog import FACTS, display_value
from svetofor.rules.model import CrossDocumentRule, Rule, SignalRule, ThresholdRule
from svetofor.rules.results import CheckContext, DocumentView
from svetofor.rules.types import EngineDeps

SANDBOX_DOC_ID = "sandbox"
SANDBOX_LABEL = "Проверяемый текст"
_PARAGRAPH = re.compile(r"\n\s*\n|\n(?=\s*\d+(?:\.\d+)*\.?\s)")
_MAX_CHUNK_CHARS = 2400


def text_chunks(text: str) -> list[SourceChunk]:
    """Текст → фрагменты по абзацам (до ~700 токенов) с точными смещениями."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _PARAGRAPH.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    chunks: list[SourceChunk] = []
    current: tuple[int, int] | None = None
    for s, e in spans:
        if not text[s:e].strip():
            continue
        if current and e - current[0] <= _MAX_CHUNK_CHARS:
            current = (current[0], e)
            continue
        if current:
            chunks.append(_chunk(text, current, len(chunks)))
        current = (s, e)
    if current:
        chunks.append(_chunk(text, current, len(chunks)))
    return chunks


def _chunk(text: str, span: tuple[int, int], index: int) -> SourceChunk:
    start, end = span
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return SourceChunk(
        id=f"s{index}",
        document_id=SANDBOX_DOC_ID,
        section_path=SANDBOX_LABEL,
        page_from=1,
        page_to=1,
        text=text[start:end],
        char_start=start,
        token_count=max(1, round((end - start) / 3.5)),
    )


def _doc_type(rule: Rule, requested: DocType | None) -> DocType:
    if requested is not None:
        return requested
    if isinstance(rule, ThresholdRule):
        sources = FACTS[rule.fact].sources
        return next((t for t in rule.applies_to if t in sources), rule.applies_to[0])
    return rule.applies_to[0]


def run_sandbox(
    rule: Rule, text: str, doc_type: DocType | None, deps: AnalysisDeps
) -> dict[str, Any]:
    if isinstance(rule, CrossDocumentRule | SignalRule):
        reason = (
            "Правило сравнивает два документа — проверьте его на пакете"
            if isinstance(rule, CrossDocumentRule)
            else "Правило проверяет оформление файла (скрытый текст) — проверьте его на пакете"
        )
        return {
            "outcome": RuleOutcome.NOT_CHECKED.value,
            "reason": reason,
            "findings": [],
            "facts": [],
        }
    kind = _doc_type(rule, doc_type)
    chunks = text_chunks(text)
    facts = DocumentFacts(SANDBOX_DOC_ID, kind)
    if isinstance(rule, ThresholdRule) and rule.fact in {f.key for f in facts_for(kind)}:
        by_rules = extract_rule_facts(SANDBOX_DOC_ID, kind, chunks)  # работает и без модели
        by_model = (
            FactExtractor(deps.gateway, deps.verifier).extract(
                document_id=SANDBOX_DOC_ID, doc_type=kind, doc_label=SANDBOX_LABEL, chunks=chunks
            )
            if deps.gateway is not None
            else DocumentFacts(SANDBOX_DOC_ID, kind, error="нет доступа к модели")
        )
        facts = merge_channels(by_rules, by_model)
    ctx = CheckContext(
        (DocumentView(SANDBOX_DOC_ID, kind, SANDBOX_LABEL, tuple(chunks)),), (facts,)
    )
    engine = RuleEngine(
        EngineDeps(verifier=deps.verifier, gateway=deps.gateway, retrieve=lambda *_: list(chunks))
    )
    probe = rule.model_copy(
        update={"enabled": True, "applies_to": [kind], "law": ["44-ФЗ", "223-ФЗ"]}
    )
    outcome = engine.run([probe], ctx)[0]
    return {
        "outcome": outcome.outcome.value,
        "reason": outcome.reason,
        "findings": [
            {
                "severity": f.severity.value,
                "kind": f.kind.value,
                "message": f.message,
                "explanation": f.explanation,
                "quote": f.evidence[0].quote if f.evidence else None,
                "char_start": f.evidence[0].char_start if f.evidence else None,
                "char_end": f.evidence[0].char_end if f.evidence else None,
            }
            for f in outcome.findings
            if f.status is FindingStatus.SHOWN
        ],
        "rejected": len(outcome.rejected),
        "facts": [
            {
                "key": f.key,
                "label": FACTS[f.key].label,
                "display": display_value(f.key, f.value),
                "quote": f.quote,
            }
            for f in facts.facts.values()
        ],
    }
