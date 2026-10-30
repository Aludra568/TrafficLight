"""Мини-язык условий правил типа threshold.

Грамматика (без eval — условия пишет методолог в интерфейсе):
    expr    := and_expr ( "or" and_expr )*
    and_expr:= cmp ( "and" cmp )*
    cmp     := "value" OP literal
    OP      := ">" | ">=" | "<" | "<=" | "==" | "!="
    literal := число | 'строка' | true | false
`and` связывает сильнее `or`, как в Python.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Literal_ = float | str | bool

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<num>-?\d+(?:[.,]\d+)?)
      | '(?P<str>[^']*)'
      | (?P<op>>=|<=|==|!=|>|<)
      | (?P<word>[A-Za-z_]+)
    )""",
    re.VERBOSE,
)

_OPS: dict[str, Callable[[Any, Any], Any]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
_ORDERING = {">", ">=", "<", "<="}


class ConditionError(ValueError):
    """Условие написано с ошибкой. Сообщение — для методолога."""


@dataclass(frozen=True)
class Comparison:
    op: str
    literal: Literal_

    def evaluate(self, value: Literal_) -> bool:
        literal = self.literal
        if isinstance(literal, bool) or isinstance(value, bool):
            if not (isinstance(literal, bool) and isinstance(value, bool)):
                return False
            if self.op not in ("==", "!="):
                return False
        elif isinstance(literal, str) or isinstance(value, str):
            if not (isinstance(literal, str) and isinstance(value, str)):
                return False
            if self.op in _ORDERING:
                return False
        return bool(_OPS[self.op](value, literal))


@dataclass(frozen=True)
class Condition:
    source: str
    any_of: tuple[tuple[Comparison, ...], ...]  # OR из AND-групп

    def evaluate(self, value: Literal_) -> bool:
        if isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        return any(all(c.evaluate(value) for c in group) for group in self.any_of)

    @property
    def literals(self) -> list[Literal_]:
        return [c.literal for group in self.any_of for c in group]


def _tokenize(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = source.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            raise ConditionError(f"Непонятный фрагмент условия: «{text[pos : pos + 15]}»")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return tokens


def _literal(kind: str, raw: str) -> Literal_:
    if kind == "num":
        return float(raw.replace(",", "."))
    if kind == "str":
        return raw
    if kind == "word" and raw in ("true", "false"):
        return raw == "true"
    raise ConditionError(f"Ожидалось число, 'строка', true или false, а не «{raw}»")


def parse_condition(source: str) -> Condition:
    tokens = _tokenize(source)
    if not tokens:
        raise ConditionError("Условие пустое")
    groups: list[tuple[Comparison, ...]] = []
    current: list[Comparison] = []
    i = 0
    while True:
        if i + 3 > len(tokens):  # сравнению нужно три токена: value, операция, значение
            raise ConditionError("Условие оборвано: нужно «value <операция> <значение>»")
        kind, word = tokens[i]
        if kind != "word" or word != "value":
            raise ConditionError(f"Каждое сравнение начинается со слова value, а не «{word}»")
        op_kind, op = tokens[i + 1]
        if op_kind != "op":
            raise ConditionError(f"После value нужна операция (>, >=, <, <=, ==, !=), а не «{op}»")
        current.append(Comparison(op, _literal(*tokens[i + 2])))
        i += 3
        if i == len(tokens):
            break
        joiner_kind, joiner = tokens[i]
        if joiner_kind != "word" or joiner not in ("and", "or"):
            raise ConditionError(f"Сравнения соединяются словами and/or, а не «{joiner}»")
        if joiner == "or":
            groups.append(tuple(current))
            current = []
        i += 1
        if i == len(tokens):
            raise ConditionError(f"После «{joiner}» нужно ещё одно сравнение")
    groups.append(tuple(current))
    return Condition(source=source.strip(), any_of=tuple(groups))
