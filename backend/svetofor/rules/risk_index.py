"""Индекс риска и рекомендация «участвовать / не участвовать».

Метод: веса категорий рисков из попарных сравнений по методу
анализа иерархий (Саати) с проверкой согласованности, уточнённые энтропийными весами по реальным
пакетам; индекс = 100 · Σ w_c · s_c, где s_c = 1 при критичном замечании, 0,5 — при «внимании».
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from svetofor.db.enums import Severity

# Случайный индекс согласованности Саати для n = 1…15.
_RANDOM_INDEX = (
    0.0,
    0.0,
    0.58,
    0.90,
    1.12,
    1.24,
    1.32,
    1.41,
    1.45,
    1.49,
    1.51,
    1.48,
    1.56,
    1.57,
    1.59,
)
CR_LIMIT = 0.10


class RiskModelError(ValueError):
    pass


@dataclass(frozen=True)
class AhpResult:
    weights: tuple[float, ...]
    lambda_max: float
    consistency_index: float
    consistency_ratio: float

    @property
    def consistent(self) -> bool:
        return self.consistency_ratio < CR_LIMIT


def ahp_weights(matrix: Sequence[Sequence[float]], iterations: int = 200) -> AhpResult:
    """Веса из матрицы попарных сравнений (главный собственный вектор) и согласованность."""
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        raise RiskModelError("Матрица сравнений должна быть квадратной и непустой")
    if n > len(_RANDOM_INDEX):
        raise RiskModelError(f"Не больше {len(_RANDOM_INDEX)} категорий в одной матрице")
    for i in range(n):
        for j in range(n):
            a = matrix[i][j]
            if a <= 0:
                raise RiskModelError("Все элементы матрицы должны быть положительными")
            if not math.isclose(a * matrix[j][i], 1.0, rel_tol=1e-6):
                raise RiskModelError(f"Матрица не обратно-симметрична: a[{i}][{j}]·a[{j}][{i}] ≠ 1")

    weights = [1.0 / n] * n
    for _ in range(iterations):
        product = [sum(matrix[i][j] * weights[j] for j in range(n)) for i in range(n)]
        total = sum(product)
        new = [p / total for p in product]
        if max(abs(a - b) for a, b in zip(new, weights, strict=True)) < 1e-12:
            weights = new
            break
        weights = new

    product = [sum(matrix[i][j] * weights[j] for j in range(n)) for i in range(n)]
    lambda_max = sum(product[i] / weights[i] for i in range(n)) / n
    ci = (lambda_max - n) / (n - 1) if n > 2 else 0.0
    ri = _RANDOM_INDEX[n - 1]
    cr = ci / ri if ri > 0 else 0.0
    return AhpResult(tuple(weights), lambda_max, ci, max(cr, 0.0))


def entropy_weights(scores: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Объективные веса по разбросу: строки — пакеты, столбцы — категории (значения ≥ 0).

    Категория, по которой все пакеты одинаковы, информации не несёт и получает вес ~0.
    """
    if not scores or not scores[0]:
        raise RiskModelError("Нужна хотя бы одна строка и один столбец")
    m, k = len(scores), len(scores[0])
    if any(len(row) != k for row in scores):
        raise RiskModelError("Строки разной длины")
    if m < 2:
        return tuple([1.0 / k] * k)
    divergence: list[float] = []
    for j in range(k):
        column = [scores[i][j] for i in range(m)]
        if any(x < 0 for x in column):
            raise RiskModelError("Значения не могут быть отрицательными")
        total = sum(column)
        if total == 0:
            divergence.append(0.0)
            continue
        entropy = -sum((x / total) * math.log(x / total) for x in column if x > 0) / math.log(m)
        divergence.append(max(0.0, 1.0 - entropy))
    total_div = sum(divergence)
    if total_div == 0:
        return tuple([1.0 / k] * k)
    return tuple(d / total_div for d in divergence)


def combine_weights(
    subjective: Sequence[float], objective: Sequence[float], alpha: float = 0.5
) -> tuple[float, ...]:
    if len(subjective) != len(objective):
        raise RiskModelError("Разное число категорий у весов")
    if not 0.0 <= alpha <= 1.0:
        raise RiskModelError("alpha должна быть от 0 до 1")
    mixed = [alpha * s + (1 - alpha) * o for s, o in zip(subjective, objective, strict=True)]
    total = sum(mixed)
    return tuple(w / total for w in mixed)


class Recommendation(StrEnum):
    PARTICIPATE = "participate"
    CAUTION = "caution"
    HIGH_RISK = "high_risk"


RECOMMENDATION_TEXT: dict[Recommendation, str] = {
    Recommendation.PARTICIPATE: "Риски обычные — можно участвовать",
    Recommendation.CAUTION: (
        "Участвовать с осторожностью: задать вопросы заказчику и заложить риски в цену"
    ),
    Recommendation.HIGH_RISK: "Высокий риск — взвесить отказ от участия",
}

SEVERITY_SCORE: dict[Severity, float] = {Severity.RED: 1.0, Severity.YELLOW: 0.5}


def category_scores(findings: Sequence[tuple[str, Severity]]) -> dict[str, float]:
    """Балл категории — по самому серьёзному замечанию в ней."""
    scores: dict[str, float] = {}
    for category, severity in findings:
        scores[category] = max(scores.get(category, 0.0), SEVERITY_SCORE[severity])
    return scores


@dataclass(frozen=True)
class RiskModel:
    weights: Mapping[str, float]
    caution_from: float = 30.0
    high_from: float = 60.0
    source: str = "равные веса (модель ещё не откалибрована)"
    explanation: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.weights:
            raise RiskModelError("Нужны веса хотя бы одной категории")
        if not math.isclose(sum(self.weights.values()), 1.0, rel_tol=1e-6):
            raise RiskModelError("Сумма весов должна быть 1")
        if not 0 <= self.caution_from < self.high_from <= 100:
            raise RiskModelError("Пороги: 0 ≤ «осторожно» < «высокий риск» ≤ 100")

    @classmethod
    def uniform(cls, categories: Sequence[str]) -> RiskModel:
        return cls(weights={c: 1.0 / len(categories) for c in categories})

    def index(self, scores: Mapping[str, float]) -> float:
        value = 100.0 * sum(w * scores.get(c, 0.0) for c, w in self.weights.items())
        return round(min(100.0, max(0.0, value)), 1)

    def contributions(self, scores: Mapping[str, float]) -> dict[str, float]:
        """Вклад каждой категории в индекс (для объяснения в интерфейсе)."""
        return {c: round(100.0 * w * scores.get(c, 0.0), 1) for c, w in self.weights.items()}

    def recommend(self, index: float, critical: int = 0) -> Recommendation:
        """Индекс меряет ширину рисков по категориям. Один критичный риск индекс почти не
        поднимает, но «можно участвовать» рядом с красным светофором — противоречие: при
        критичных замечаниях рекомендация не мягче «с осторожностью»."""
        if index >= self.high_from:
            return Recommendation.HIGH_RISK
        if index >= self.caution_from or critical > 0:
            return Recommendation.CAUTION
        return Recommendation.PARTICIPATE
