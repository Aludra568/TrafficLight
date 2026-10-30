"""Доверительные интервалы бутстрэпом по пакетам.

Пакеты выбираются с возвращением (пакет — единица случайности: риски внутри пакета связаны),
метрика считается заново, интервал — по перцентилям. Зерно фиксировано: цифры в README
воспроизводимы.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence


def bootstrap_ci[T](
    items: Sequence[T],
    metric: Callable[[Sequence[T]], float | None],
    *,
    repeats: int = 1000,
    level: float = 0.95,
    seed: int = 20261101,
) -> tuple[float, float] | None:
    if not items:
        return None
    rng = random.Random(seed)  # noqa: S311 — статистика, не криптография
    values: list[float] = []
    for _ in range(repeats):
        sample = [items[rng.randrange(len(items))] for _ in items]
        value = metric(sample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    tail = (1 - level) / 2
    low = values[int(tail * (len(values) - 1))]
    high = values[int((1 - tail) * (len(values) - 1))]
    return round(low, 3), round(high, 3)
