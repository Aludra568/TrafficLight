"""Выражения правил, написанные методологом, — с настоящим ограничением времени.

Выражение правила пишет человек, а выполняется оно на чужом документе. Стандартный `re` нельзя
прервать: повтор, который совпадает одним и тем же текстом разными способами («(а|аа)+$»),
на длинном документе думает минутами и держит воркер — мягкий лимит задачи Celery не срабатывает,
пока поиск идёт внутри C-кода. Пакет `regex` (синтаксис `re`, режим V0) принимает `timeout=`
на каждый поиск: время правила ограничено, а не превысившее его правило честно «не проверено»
(сбой, а не «в порядке»), и красное правило тогда не даёт пакету стать зелёным.

Статическая проверка при сохранении (`model.nested_quantifier`) остаётся — она объясняет, что
не так, ещё до запуска. Здесь — то, что статикой не поймать: прогон на «тяжёлых» строках.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, cast

import regex

__all__ = [
    "PATTERN_TIME_BUDGET_S",
    "RuleRegex",
    "RuleRegexTimeoutError",
    "TimeBudget",
    "compile_rule_pattern",
    "too_slow",
]

# сколько всего может искать одно правило по всему пакету (все документы и фрагменты)
PATTERN_TIME_BUDGET_S = 3.0
# проверка при сохранении: одно выражение на одной «тяжёлой» строке
_STRESS_TIMEOUT_S = 0.2
_STRESS_LENGTH = 3000


class RuleRegexTimeoutError(Exception):
    """Выражение правила не уложилось во время."""


@dataclass(frozen=True)
class RuleRegex:
    """Скомпилированное выражение правила: поиск только с ограничением времени."""

    source: str
    _compiled: Any = field(repr=False)

    def search(self, text: str, budget: TimeBudget) -> tuple[int, int] | None:
        """Первое непустое совпадение (смещения) или None. Время вышло — `RuleRegexTimeoutError`."""
        remaining = budget.remaining()
        if remaining <= 0:
            raise RuleRegexTimeoutError(self.source)
        try:
            match = self._compiled.search(text, timeout=remaining)
        except TimeoutError as exc:
            budget.exhaust()  # движок прерывает чуть раньше срока: остаток — уже не запас
            raise RuleRegexTimeoutError(self.source) from exc
        if match is None or match.end() <= match.start():
            return None
        return cast(int, match.start()), cast(int, match.end())


@dataclass
class TimeBudget:
    """Общий запас времени на правило: каждый поиск получает остаток, а не полный лимит."""

    seconds: float = PATTERN_TIME_BUDGET_S
    _deadline: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._deadline = time.monotonic() + self.seconds

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def exhaust(self) -> None:
        self._deadline = time.monotonic()


def compile_rule_pattern(pattern: str) -> RuleRegex:
    """Синтаксис как у `re` (режим V0); ошибка синтаксиса — `regex.error`."""
    return RuleRegex(pattern, regex.compile(pattern, regex.V0))


def _stress_texts(pattern: str) -> list[str]:
    """Строки, на которых проявляется перебор: длинные повторы символов из самого выражения
    и типичных классов (буквы, цифры, пробелы), без завершающего совпадения."""
    literal = {ch for ch in pattern.lower() if ch.isalnum() or ch == " "}
    chars = ["а", "1", " ", "x", *sorted(literal)[:12]]
    texts = [ch * _STRESS_LENGTH for ch in dict.fromkeys(chars)]
    texts.append("аб " * (_STRESS_LENGTH // 3))
    return [text + "⁣" for text in texts]  # невидимый разделитель: «конца» совпадения нет


def too_slow(pattern: str) -> bool:
    """Выражение думает дольше `_STRESS_TIMEOUT_S` хотя бы на одной «тяжёлой» строке."""
    compiled = regex.compile(pattern, regex.V0)
    for text in _stress_texts(pattern):
        try:
            compiled.search(text, timeout=_STRESS_TIMEOUT_S)
        except TimeoutError:
            return True
    return False
