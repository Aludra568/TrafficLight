"""Типы правил и их реестр.

Новый тип правила — модуль здесь с классом-наследником `RuleEvaluator`, помеченным
`@register("имя")`, плюс Pydantic-модель в `rules/model.py`. Движок находит его сам.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from svetofor.db.enums import DocType
from svetofor.llm.gateway import LLMGateway
from svetofor.rules.facts import SourceChunk
from svetofor.rules.results import CheckContext, RuleOutcomeDraft
from svetofor.rules.verify import QuoteVerifier


@dataclass(frozen=True)
class SearchRequest:
    """Запрос поиска от одного правила: текст для вектора и точные формулировки для полнотекста."""

    text: str
    hints: tuple[str, ...]


class RetrievedChunks(list[SourceChunk]):
    """Фрагменты поиска и признак: участвовал ли смысловой (векторный) поиск.

    Без него поиск идёт только по словам и пропускает перефразировки («отказаться» вместо
    «односторонний отказ») — «в порядке» по такому набору фрагментов не доказано.
    """

    semantic: bool = True


class Retriever(Protocol):
    """(типы документов, запросы правил группы[, бюджет токенов]) → фрагменты для промпта."""

    def __call__(
        self,
        doc_types: Sequence[DocType],
        requests: Sequence[SearchRequest],
        budget: int | None = None,
    ) -> list[SourceChunk]: ...


@dataclass
class EngineDeps:
    verifier: QuoteVerifier
    gateway: LLMGateway | None = None
    retrieve: Retriever | None = None
    check_id: uuid.UUID | None = None
    max_rules_per_call: int = 6
    whole_budget: int = 12_000  # «документ целиком» во втором проходе (Budgets.whole)
    second_opinion: bool = False  # перепроверка красных нарушений модели (LLM_SECOND_OPINION)


class RuleEvaluator(ABC):
    def __init__(self, deps: EngineDeps) -> None:
        self.deps = deps

    @abstractmethod
    def evaluate(self, rules: Sequence[Any], ctx: CheckContext) -> list[RuleOutcomeDraft]:
        """Исходы для переданных правил своего типа — по одному на правило."""


RULE_TYPES: dict[str, type[RuleEvaluator]] = {}


def register(name: str) -> Callable[[type[RuleEvaluator]], type[RuleEvaluator]]:
    def decorator(cls: type[RuleEvaluator]) -> type[RuleEvaluator]:
        if name in RULE_TYPES:
            raise ValueError(f"Тип правила «{name}» уже зарегистрирован")
        RULE_TYPES[name] = cls
        return cls

    return decorator


def load_builtin_types() -> None:
    """Импорт модулей регистрирует типы (декоратор срабатывает при импорте)."""
    from svetofor.rules.types import (  # noqa: F401
        cross_document,
        internal_consistency,
        llm_check,
        pattern,
        signal,
        threshold,
    )
