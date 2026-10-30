"""Поштучная проверка пунктов — «ворота» зелёного.

Для правил-запретов «в порядке» — суждение модели над всем текстом: цитата нормального пункта не
доказывает, что плохого пункта нет в другом месте. Здесь перечень пунктов строит код (все
предложения и строки таблиц документов, к которым относятся правила «критично»), модель отвечает
по каждому пункту отдельно, а код сверяет, что ответ пришёл по каждому:

- пункт без ответа (после одного повторного вопроса) → правило «не проверено»;
- отмеченный пункт → вопрос правила по пункту и его окружению: подтвердилось — нарушение с цитатой
  из документа; нет — «не проверено» с цитатой пункта (две проверки разошлись, решает человек);
- ответы по всем пунктам, ни один не отмечен → «в порядке» на основании покрытия.

Поштучная проверка не снимает блокировок (указания программам проверки, нечитаемый текст, сбой
модели): такие правила сюда не попадают. Снимается только «неполный поиск» — пункты перебираются
все, поиск не нужен. Риск, распределённый по пунктам без ссылки, она не видит (B44, раздел 13).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from typing import Any

from svetofor.db.enums import DocType, FindingKind, RuleOutcome
from svetofor.llm.errors import LLMError
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import render
from svetofor.llm.schemas import SweepResult, SweepVerdict
from svetofor.rules.aggregate import can_be_red
from svetofor.rules.facts import SourceChunk
from svetofor.rules.model import LlmCheckRule, Rule
from svetofor.rules.results import (
    BASIS_COVERAGE,
    TRUSTED_BASES,
    CheckContext,
    DocumentView,
    Evidence,
    FindingDraft,
    RuleOutcomeDraft,
)
from svetofor.rules.templates import render as render_message
from svetofor.rules.types.pattern import sentence_bounds

SWEEP_TASK = "sweep"
SWEEP_TEMPLATE = "sweep.v1"
BATCH_SIZE = 30
_TOKENS_PER_UNIT = 60
_MIN_UNIT = 40  # короче — заголовок или обрывок: присоединяется к соседнему пункту
_MAX_UNIT = 1200  # длиннее — таблица или сплошной текст: режется по строкам
_MAX_REFERENCE = 600
_REFERENCE = re.compile(r"(?:пункт\w*|п\.)\s*(\d{1,2}(?:\.\d{1,3}){1,4})", re.IGNORECASE)
_NUMBER = re.compile(r"\s*(\d{1,2}(?:\.\d{1,3}){1,4})\.?\s")
_ALIAS = re.compile(r"^\[?\s*[UuУу]?\s*(\d{1,4})\s*\]?$")
_PIECE = "#unit"

Recheck = Callable[
    [Sequence[LlmCheckRule], Sequence[SourceChunk], CheckContext], list[RuleOutcomeDraft]
]


@dataclass(frozen=True)
class Unit:
    """Пункт перечня: предложение или строка таблицы (смещения — внутри текста фрагмента)."""

    document_id: str
    doc_type: DocType
    label: str
    chunk: SourceChunk
    start: int
    end: int
    reference: Unit | None = None  # пункт, на который этот ссылается («в пункте 8.2»)

    @property
    def text(self) -> str:
        return self.chunk.text[self.start : self.end]

    @property
    def page(self) -> int:
        return self.chunk.page_from

    def evidence(self) -> Evidence:
        return Evidence.from_chunk(self.chunk, self.start, self.end)

    def piece(self) -> SourceChunk:
        """Сам пункт как фрагмент для вопроса правила (цитата сверяется с ним точно)."""
        return replace(
            self.chunk,
            id=self.chunk.id + _PIECE,
            text=self.text,
            char_start=self.chunk.char_start + self.start,
        )


@dataclass(frozen=True)
class SweepDeps:
    gateway: LLMGateway
    recheck: Recheck
    check_id: uuid.UUID | None = None
    concurrency: int = 4
    batch_size: int = BATCH_SIZE


@dataclass(frozen=True)
class SweepReport:
    outcomes: list[RuleOutcomeDraft]  # findings — только новые замечания поштучной проверки
    stats: dict[str, Any]


# ─────────────────────────── перечень пунктов ───────────────────────────


def _strip(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    result = []
    while end - start > _MAX_UNIT:
        cut = text.rfind("\n", start + _MIN_UNIT, start + _MAX_UNIT)
        if cut == -1:
            cut = text.rfind(" ", start + _MIN_UNIT, start + _MAX_UNIT)
        if cut == -1:
            cut = start + _MAX_UNIT
        result.append(_strip(text, start, cut))
        start = cut
    result.append(_strip(text, start, end))
    return [(a, b) for a, b in result if a < b]


def _merge_short(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Короткий кусок (заголовок раздела) — в конец предыдущего пункта, чтобы следующий
    начинался со своего номера; в начале фрагмента — в начало следующего."""
    result: list[tuple[int, int]] = []
    pending: int | None = None
    for start, end in spans:
        if pending is not None:
            start, pending = pending, None
        if end - start >= _MIN_UNIT:
            result.append((start, end))
        elif result:
            result[-1] = (result[-1][0], end)
        else:
            pending = start
    if pending is not None:
        result.append((pending, spans[-1][1]))
    return result


def unit_spans(text: str) -> list[tuple[int, int]]:
    """Пункты текста фрагмента: предложения, начала нумерованных пунктов, строки длинных таблиц."""
    bounds = sentence_bounds(text)
    spans: list[tuple[int, int]] = []
    for left, right in pairwise(bounds):
        start, end = _strip(text, left, right)
        if start < end:
            spans.extend(_split_long(text, start, end))
    return _merge_short(spans)


def _with_references(units: list[Unit]) -> list[Unit]:
    """«В размере, установленном пунктом 8.2» — вместе с текстом пункта 8.2 того же документа."""
    numbered: dict[str, Unit] = {}
    for unit in units:
        match = _NUMBER.match(unit.text)
        if match:
            numbered.setdefault(match.group(1), unit)
    result = []
    for unit in units:
        own = _NUMBER.match(unit.text)
        refs = [
            numbered[n]
            for n in dict.fromkeys(_REFERENCE.findall(unit.text))
            if n in numbered and (own is None or n != own.group(1))
        ]
        result.append(replace(unit, reference=refs[0]) if refs else unit)
    return result


def build_units(documents: Sequence[DocumentView]) -> list[Unit]:
    """Перечень пунктов документов. Фрагменты перекрываются — пункт попадает в перечень один раз."""
    units: list[Unit] = []
    for document in documents:
        own: list[Unit] = []
        covered = -1
        for chunk in sorted(document.chunks, key=lambda c: c.char_start):
            for start, end in unit_spans(chunk.text):
                if chunk.char_start + end <= covered:
                    continue
                # фрагмент оборвал предложение, а следующий содержит его целиком — берём целое
                while own and own[-1].chunk.char_start + own[-1].start >= chunk.char_start + start:
                    own.pop()
                own.append(Unit(document.id, document.doc_type, document.label, chunk, start, end))
                covered = max(covered, chunk.char_start + end)
        units.extend(_with_references(own))
    return units


# ─────────────────────────── вопросы модели ───────────────────────────


def _parallel[T, R](fn: Callable[[T], R], items: Sequence[T], workers: int) -> list[R]:
    if len(items) <= 1 or workers <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def _alias(unit_id: str) -> str | None:
    match = _ALIAS.match(unit_id.strip())
    return f"U{int(match.group(1))}" if match else None


def _ask(rules: Sequence[LlmCheckRule], units: Sequence[Unit], deps: SweepDeps) -> dict[int, Any]:
    """Ответы по пачке: номер пункта в пачке → вердикт. Сбой модели — ответов нет."""
    shown = [
        {
            "alias": f"U{i}",
            "label": unit.label,
            "section_path": unit.chunk.section_path,
            "page": unit.page,
            "text": unit.text,
            "reference": unit.reference.text[:_MAX_REFERENCE] if unit.reference else "",
        }
        for i, unit in enumerate(units, start=1)
    ]
    prompt = render(SWEEP_TEMPLATE, rules=rules, units=shown)
    try:
        answer = deps.gateway.complete_json(
            task=SWEEP_TASK,
            model_role="main",
            system=prompt.system,
            user=prompt.user,
            schema=SweepResult,
            prompt_version=prompt.version,
            max_tokens=_TOKENS_PER_UNIT * len(units) + 300,
            check_id=deps.check_id,
        )
    except LLMError:
        return {}
    result: dict[int, SweepVerdict] = {}
    for verdict in answer.value.units:
        alias = _alias(verdict.unit_id)
        if alias is not None and 1 <= (index := int(alias[1:])) <= len(units):
            result.setdefault(index - 1, verdict)
    return result


def _ask_all(
    targets: Sequence[LlmCheckRule], units: Sequence[Unit], deps: SweepDeps
) -> dict[int, SweepVerdict]:
    """Ответы по всем пунктам; пропущенные номера — один повторный вопрос."""
    answers: dict[int, SweepVerdict] = {}
    by_id = {r.id: r for r in targets}

    def ask(batch: tuple[tuple[str, ...], list[int]]) -> dict[int, Any]:
        key, indices = batch
        return _ask([by_id[k] for k in key], [units[i] for i in indices], deps)

    for _ in range(2):
        pending: dict[tuple[str, ...], list[int]] = {}
        for i, unit in enumerate(units):
            if i not in answers:
                key = tuple(r.id for r in targets if unit.doc_type in r.applies_to)
                pending.setdefault(key, []).append(i)
        batches = [
            (key, indices[s : s + deps.batch_size])
            for key, indices in pending.items()
            for s in range(0, len(indices), deps.batch_size)
        ]
        if not batches:
            break
        for (_, indices), got in zip(
            batches, _parallel(ask, batches, deps.concurrency), strict=True
        ):
            for local, verdict in got.items():
                answers[indices[local]] = verdict
    return answers


def _flagged(
    answers: dict[int, SweepVerdict], units: Sequence[Unit], targets: Sequence[LlmCheckRule]
) -> dict[int, list[LlmCheckRule]]:
    """Отмеченные пункты и их правила. «Не уверена» без правил — все правила документа."""
    result: dict[int, list[LlmCheckRule]] = {}
    for i, verdict in answers.items():
        if verdict.verdict == "none":
            continue
        own = [r for r in targets if units[i].doc_type in r.applies_to]
        named = {code.strip() for code in verdict.rule_ids}
        result[i] = [r for r in own if r.id in named] or own
    return result


def _recheck_all(
    flagged: dict[int, list[LlmCheckRule]],
    units: Sequence[Unit],
    ctx: CheckContext,
    deps: SweepDeps,
) -> dict[int, dict[str, RuleOutcomeDraft]]:
    """Вопрос правил по отмеченному пункту, его фрагменту и пункту, на который он ссылается."""

    def ask(i: int) -> dict[str, RuleOutcomeDraft]:
        unit = units[i]
        chunks = [unit.piece(), unit.chunk]
        if unit.reference is not None and unit.reference.chunk.id != unit.chunk.id:
            chunks.append(unit.reference.chunk)
        try:
            return {o.rule_id: o for o in deps.recheck(flagged[i], chunks, ctx)}
        except LLMError:
            return {}

    indices = list(flagged)
    return dict(zip(indices, _parallel(ask, indices, deps.concurrency), strict=True))


# ─────────────────────────── исход правила ───────────────────────────


def sweepable(outcome: RuleOutcomeDraft) -> bool:
    """Исход, который поштучная проверка может подтвердить или опровергнуть."""
    if outcome.outcome in (RuleOutcome.OK, RuleOutcome.ABSENT):
        return True
    details = outcome.details or {}
    return outcome.outcome is RuleOutcome.NOT_CHECKED and details.get("blocked") == "partial_search"


def sweep_targets(
    rules: Sequence[Rule], outcomes: Sequence[RuleOutcomeDraft]
) -> list[LlmCheckRule]:
    """Смысловые правила, способные дать «критично» (не список кодов: критерий 2 ТЗ)."""
    by_id = {o.rule_id: o for o in outcomes}
    return [
        r
        for r in rules
        if isinstance(r, LlmCheckRule)
        and r.enabled
        and can_be_red(r)
        and r.id in by_id
        and sweepable(by_id[r.id])
    ]


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _sweep_finding(rule: LlmCheckRule, unit: Unit) -> FindingDraft:
    evidence = unit.evidence()
    return FindingDraft(
        rule_id=rule.id,
        kind=FindingKind.VIOLATION,
        severity=rule.severity,
        title=rule.title,
        message=render_message(rule.message, quote=evidence.quote),
        evidence=(evidence,),
        document_id=unit.document_id,
        related={"sweep": True},
    )


def _confirmed(
    rule: LlmCheckRule, unit: Unit, answer: RuleOutcomeDraft | None
) -> list[FindingDraft]:
    """Нарушение подтвердилось вопросом правила. Цитата — проверенная цитата модели, а если она
    не нашлась, — сам пункт: о нарушении в нём сказали обе проверки."""
    if answer is None:
        return []
    if answer.outcome is RuleOutcome.VIOLATION:
        return [
            replace(
                f,
                evidence=tuple(
                    replace(e, chunk_id=unit.chunk.id) if (e.chunk_id or "").endswith(_PIECE) else e
                    for e in f.evidence
                ),
                related={**(f.related or {}), "sweep": True},
            )
            for f in answer.shown
        ]
    if (answer.details or {}).get("error") == "unverified_violation":
        return [_sweep_finding(rule, unit)]
    return []


def _decide(
    rule: LlmCheckRule,
    outcome: RuleOutcomeDraft,
    units: Sequence[Unit],
    answers: dict[int, SweepVerdict],
    flagged: dict[int, list[LlmCheckRule]],
    rechecked: dict[int, dict[str, RuleOutcomeDraft]],
) -> RuleOutcomeDraft:
    own = [i for i, u in enumerate(units) if u.doc_type in rule.applies_to]
    marked = [i for i in own if any(r.id == rule.id for r in flagged.get(i, ()))]
    missing = [i for i in own if i not in answers]
    findings: list[FindingDraft] = []
    disputed: list[Unit] = []
    for i in marked:
        found = _confirmed(rule, units[i], rechecked.get(i, {}).get(rule.id))
        if found:
            findings.extend(found)
        else:
            disputed.append(units[i])
    summary: dict[str, Any] = {
        "units": len(own),
        "flagged": len(marked),
        "confirmed": len(marked) - len(disputed),
        "missing": len(missing),
    }
    was = outcome.outcome.value
    if findings:
        return RuleOutcomeDraft(
            rule.id, RuleOutcome.VIOLATION, None, tuple(findings), {"sweep": summary, "was": was}
        )
    if outcome.outcome is RuleOutcome.ABSENT or not own:
        # условия нет — это уже замечание; пунктов нет — подтверждать нечем
        return outcome.with_details(sweep=summary)
    if disputed:
        unit = disputed[0]
        return RuleOutcomeDraft(
            rule.id,
            RuleOutcome.NOT_CHECKED,
            f"поштучная проверка отметила пункт «{_short(unit.text)}» (стр. {unit.page}), а "
            "проверка правила его не подтвердила — проверьте вручную",
            details={
                "blocked": "sweep_disputed",
                "was": was,
                "sweep": {**summary, "disputed": [asdict(u.evidence()) for u in disputed]},
            },
        )
    if missing:
        labels = ", ".join(dict.fromkeys(f"«{units[i].label}»" for i in missing))
        return RuleOutcomeDraft(
            rule.id,
            RuleOutcome.NOT_CHECKED,
            f"поштучная проверка не получила ответа по {len(missing)} из {len(own)} пунктов "
            f"({labels}) — отсутствие риска не подтверждено",
            details={"blocked": "sweep_incomplete", "was": was, "sweep": summary},
        )
    # ответ по каждому пункту, ни один не нарушает правило: «в порядке» доказано покрытием
    details = dict(outcome.details or {})
    lifted = details.pop("blocked", None)
    details.pop("was", None)
    if details.get("basis") not in TRUSTED_BASES:
        details["basis"] = BASIS_COVERAGE
    details["sweep"] = summary
    if lifted:
        details["lifted"] = lifted
    return RuleOutcomeDraft(rule.id, RuleOutcome.OK, None, (), details)


def sweep(
    outcomes: Sequence[RuleOutcomeDraft],
    rules: Sequence[Rule],
    ctx: CheckContext,
    deps: SweepDeps,
) -> SweepReport:
    """Поштучная проверка пунктов по правилам «критично» с исходом «в порядке» или «не найдено»."""
    targets = sweep_targets(rules, outcomes)
    if not targets:
        return SweepReport(list(outcomes), {"rules": [], "units": 0})
    types = list(dict.fromkeys(t for r in targets for t in r.applies_to))
    documents = [d for d in ctx.documents_of(types) if not d.instructions]
    units = build_units(documents)
    answers = _ask_all(targets, units, deps)
    flagged = _flagged(answers, units, targets)
    rechecked = _recheck_all(flagged, units, ctx, deps)
    by_id = {r.id: r for r in targets}
    result = [
        _decide(by_id[o.rule_id], o, units, answers, flagged, rechecked)
        if o.rule_id in by_id
        else o
        for o in outcomes
    ]
    changed = {o.rule_id: o for o in result if o.rule_id in by_id}
    stats = {
        "rules": list(by_id),
        "documents": [d.label for d in documents],
        "units": len(units),
        "answered": len(answers),
        "flagged": len(flagged),
        "violations": sorted(c for c, o in changed.items() if o.outcome is RuleOutcome.VIOLATION),
        "unknown": sorted(c for c, o in changed.items() if o.outcome is RuleOutcome.NOT_CHECKED),
    }
    return SweepReport(result, stats)
