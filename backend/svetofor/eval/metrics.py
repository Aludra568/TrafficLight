"""Метрики качества.

Единица счёта — пара (пакет, правило): ожидаемый риск есть/нет против найденного.
«Не проверено» — не находка: для полноты это пропуск, для точности не считается.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PairResult:
    rule: str
    expected: bool
    predicted: bool
    not_checked: bool = False
    location_ok: bool | None = None  # для верно найденного риска с эталонным фрагментом
    # смысловое правило: эталонный фрагмент был среди фрагментов, которые видела модель
    # (качество поиска отдельно от модели; None — поиск не запускался)
    context_ok: bool | None = None


def _same(expected: object, actual: object) -> bool:
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return float(expected) == float(actual)
    return expected == actual


@dataclass(frozen=True)
class FactResult:
    key: str
    expected: object
    actual: object
    # что дал бы каждый канал карточки сам по себе (fact_rules.py — грамматики, facts.py — модель)
    by_rules: object = None
    by_model: object = None
    conflict: bool = False  # каналы нашли разные значения

    @property
    def ok(self) -> bool:
        return _same(self.expected, self.actual)

    @property
    def rules_ok(self) -> bool:
        return _same(self.expected, self.by_rules)

    @property
    def model_ok(self) -> bool:
        return _same(self.expected, self.by_model)


@dataclass
class PackageResult:
    package: str
    pairs: list[PairResult] = field(default_factory=list)
    facts: list[FactResult] = field(default_factory=list)
    seconds: float = 0.0
    failed: bool = False  # проверка не дошла до конца или документ не обработан
    quotes_shown: int = 0
    quotes_rejected: int = 0


@dataclass(frozen=True)
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None


def confusion(pairs: Sequence[PairResult]) -> Confusion:
    tp = sum(1 for p in pairs if p.expected and p.predicted)
    fp = sum(1 for p in pairs if not p.expected and p.predicted)
    fn = sum(1 for p in pairs if p.expected and not p.predicted)
    tn = sum(1 for p in pairs if not p.expected and not p.predicted)
    return Confusion(tp, fp, fn, tn)


def _pairs(results: Sequence[PackageResult]) -> list[PairResult]:
    return [pair for result in results for pair in result.pairs]


def recall(results: Sequence[PackageResult]) -> float | None:
    return confusion(_pairs(results)).recall


def precision(results: Sequence[PackageResult]) -> float | None:
    return confusion(_pairs(results)).precision


def location_match(results: Sequence[PackageResult]) -> float | None:
    checked = [p.location_ok for p in _pairs(results) if p.location_ok is not None]
    return sum(checked) / len(checked) if checked else None


def _facts(results: Sequence[PackageResult]) -> list[FactResult]:
    return [f for r in results for f in r.facts]


def context_recall(results: Sequence[PackageResult]) -> float | None:
    checked = [p.context_ok for p in _pairs(results) if p.context_ok is not None]
    return sum(checked) / len(checked) if checked else None


def facts_accuracy(results: Sequence[PackageResult]) -> float | None:
    facts = _facts(results)
    return sum(f.ok for f in facts) / len(facts) if facts else None


def facts_accuracy_rules(results: Sequence[PackageResult]) -> float | None:
    facts = _facts(results)
    return sum(f.rules_ok for f in facts) / len(facts) if facts else None


def facts_accuracy_model(results: Sequence[PackageResult]) -> float | None:
    facts = _facts(results)
    return sum(f.model_ok for f in facts) / len(facts) if facts else None


def channel_conflicts(results: Sequence[PackageResult]) -> dict[str, int]:
    """Расхождения каналов карточки и кто из них прав — проверка правила «показываем модель»."""
    conflicts = [f for f in _facts(results) if f.conflict]
    return {
        "total": len(conflicts),
        "model_right": sum(f.model_ok for f in conflicts),
        "rules_right": sum(f.rules_ok for f in conflicts),
    }


def quotes_verified(results: Sequence[PackageResult]) -> float | None:
    shown = sum(r.quotes_shown for r in results)
    total = shown + sum(r.quotes_rejected for r in results)
    return shown / total if total else None


def reliability(results: Sequence[PackageResult]) -> float | None:
    return sum(not r.failed for r in results) / len(results) if results else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def timing(results: Sequence[PackageResult]) -> dict[str, float | None]:
    seconds = [r.seconds for r in results]
    return {
        "p50": percentile(seconds, 0.5),
        "p95": percentile(seconds, 0.95),
        "mean": statistics.fmean(seconds) if seconds else None,
    }


def per_rule(results: Sequence[PackageResult]) -> dict[str, dict[str, object]]:
    by_rule: dict[str, list[PairResult]] = {}
    for pair in _pairs(results):
        by_rule.setdefault(pair.rule, []).append(pair)
    table: dict[str, dict[str, object]] = {}
    for rule, pairs in sorted(by_rule.items()):
        c = confusion(pairs)
        table[rule] = {
            "tp": c.tp,
            "fp": c.fp,
            "fn": c.fn,
            "tn": c.tn,
            "recall": c.recall,
            "precision": c.precision,
            "not_checked": sum(p.not_checked for p in pairs),
        }
    return table


METRICS = {
    "recall": recall,
    "precision": precision,
    "location_match": location_match,
    "context_recall": context_recall,
    "facts_accuracy": facts_accuracy,
    "facts_accuracy_rules": facts_accuracy_rules,
    "facts_accuracy_model": facts_accuracy_model,
    "quotes_verified": quotes_verified,
    "reliability": reliability,
}
