"""Модель языка правил. Контракт между YAML, БД, интерфейсом и движком.

Валидация ловит ошибки методолога при сохранении правила, а не во время проверки пакета.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

import regex
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from svetofor.db.enums import DocType, RuleStatus, Severity
from svetofor.rules.conditions import ConditionError, parse_condition
from svetofor.rules.facts_catalog import FACTS, FactKind
from svetofor.rules.rule_regex import compile_rule_pattern, too_slow
from svetofor.rules.templates import TemplateError, template_fields


class RuleCategory(StrEnum):
    TERMS = "terms"
    PAYMENT = "payment"
    SECURITY = "security"
    LIABILITY = "liability"
    TERMINATION = "termination"
    ACCEPTANCE = "acceptance"
    REQUIREMENTS = "requirements"
    SUBJECT = "subject"
    VOLUME = "volume"
    WARRANTY = "warranty"
    NATIONAL_REGIME = "national_regime"
    CONSISTENCY = "consistency"


CATEGORY_TITLES: dict[RuleCategory, str] = {
    RuleCategory.TERMS: "Сроки",
    RuleCategory.PAYMENT: "Оплата и аванс",
    RuleCategory.SECURITY: "Обеспечение",
    RuleCategory.LIABILITY: "Неустойки и ответственность",
    RuleCategory.TERMINATION: "Расторжение и подписание",
    RuleCategory.ACCEPTANCE: "Приёмка",
    RuleCategory.REQUIREMENTS: "Требования к участнику",
    RuleCategory.SUBJECT: "Предмет закупки",
    RuleCategory.VOLUME: "Объём и логистика",
    RuleCategory.WARRANTY: "Гарантия",
    RuleCategory.NATIONAL_REGIME: "Нацрежим",
    RuleCategory.CONSISTENCY: "Противоречия в документах",
}


class AbsentBehavior(StrEnum):
    IGNORE = "ignore"
    REPORT = "report"


class LegalRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    act: str = Field(min_length=1, max_length=200)
    ref: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=500)
    edition_checked: date | None = None


class ThresholdCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str
    severity: Severity

    @field_validator("when")
    @classmethod
    def _valid_syntax(cls, value: str) -> str:
        try:
            parse_condition(value)
        except ConditionError as exc:
            raise ValueError(str(exc)) from exc
        return value.strip()


class OnMissing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    message: str = Field(min_length=3, max_length=500)


_RULE_ID = r"^[A-Z]{2,5}-\d{2,3}$"


class RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    id: str = Field(pattern=_RULE_ID, description="Стабильный код, например PAY-01")
    title: str = Field(min_length=3, max_length=200)
    category: RuleCategory
    law: list[str] = Field(min_length=1)
    applies_to: list[DocType] = Field(min_length=1)
    enabled: bool = True
    message: str = Field(min_length=3, max_length=500)
    why_risk: str = Field(min_length=3, max_length=2000)
    recommendation: str = Field(min_length=3, max_length=2000)
    # вопрос заказчику для черновика запроса на разъяснение; подстановки {quote} и {value}
    clarification: str | None = Field(None, max_length=1000)
    legal_basis: list[LegalRef] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    status: RuleStatus = RuleStatus.DRAFT
    verified_by: str | None = None
    verified_at: date | None = None

    @field_validator("message", "why_risk", "recommendation", "clarification")
    @classmethod
    def _safe_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            template_fields(value)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
        return value.strip()

    @model_validator(mode="after")
    def _verified_needs_who_and_when(self) -> RuleBase:
        if self.status is RuleStatus.VERIFIED and not (self.verified_by and self.verified_at):
            raise ValueError(
                "Для статуса «проверено» укажите, кто проверил (verified_by) и когда (verified_at)"
            )
        return self

    @property
    def code(self) -> str:
        return self.id


# группа, внутри которой есть + или * (не в классе символов), и сразу за ней + * или {n,}
_NESTED_QUANTIFIER = re.compile(
    r"\((?:[^()\\\[]|\\.|\[(?:[^\]\\]|\\.)*\])*[+*](?:[^()\\\[]|\\.|\[(?:[^\]\\]|\\.)*\])*\)"
    r"(?:[+*]|\{\d*,\d*\})"
)


def nested_quantifier(pattern: str) -> bool:
    """Повтор внутри повтора — главный источник катастрофического перебора в регулярках.

    Правило с таким выражением на длинном документе может «думать» минутами и задержать
    проверку всего пакета. Проверка простая и строгая: лучше попросить переписать выражение.
    """
    return _NESTED_QUANTIFIER.search(pattern) is not None


def _timed(info: ValidationInfo) -> bool:
    """Замер времени выражений — только при записи правила (редактор, импорт, YAML). Правило,
    уже сохранённое в базе, читается без замера (`store.rule_from_body`, контекст `stored`):
    под нагрузкой замер ложно не укладывался в лимит и ронял всю группу правил, а во время
    работы поиск и так ограничен временем (`rule_regex.TimeBudget`)."""
    return not (isinstance(info.context, dict) and info.context.get("stored"))


def check_patterns(values: list[str], *, timed: bool = True) -> list[str]:
    """Выражения правила: синтаксис движка `regex`, без перебора на длинном тексте."""
    for pattern in values:
        try:
            compile_rule_pattern(pattern)  # тот же движок, которым правило выполняется
        except regex.error as exc:
            raise ValueError(f"Ошибка в регулярном выражении «{pattern}»: {exc}") from exc
        if nested_quantifier(pattern):
            raise ValueError(
                f"Выражение «{pattern}» может работать очень долго: повтор внутри повтора "
                "(например, «(а+)+» или «(\\w*)*»). Уберите внешний или внутренний повтор"
            )
        if timed and too_slow(pattern):
            raise ValueError(
                f"Выражение «{pattern}» работает слишком долго на длинном тексте: разные "
                "варианты повтора совпадают с одним и тем же текстом (например, «(а|аа)+»). "
                "Упростите повтор"
            )
    return values


class PatternRule(RuleBase):
    type: Literal["pattern"]
    patterns: list[str] = Field(min_length=1)
    exclude_patterns: list[str] = Field(default_factory=list)
    scope: Literal["chunk", "document"] = "chunk"
    severity: Severity

    @field_validator("patterns", "exclude_patterns")
    @classmethod
    def _valid_regex(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return check_patterns(values, timed=_timed(info))


def _check_fact_literals(fact: str, conditions: list[ThresholdCondition]) -> None:
    spec = FACTS.get(fact)
    if spec is None:
        raise ValueError(f"Неизвестный факт «{fact}». Допустимые: {', '.join(FACTS)}")
    for cond in conditions:
        for literal in parse_condition(cond.when).literals:
            if spec.kind is FactKind.NUMBER and (
                isinstance(literal, bool) or not isinstance(literal, float)
            ):
                raise ValueError(f"Факт «{spec.label}» — число, а в условии «{cond.when}» не число")
            if spec.kind is FactKind.BOOL and not isinstance(literal, bool):
                raise ValueError(f"Факт «{spec.label}» — да/нет, в условии нужно true или false")
            if spec.kind is FactKind.ENUM and literal not in spec.choices:
                allowed = ", ".join(f"'{c}'" for c in spec.choices)
                raise ValueError(f"Для факта «{spec.label}» допустимы значения: {allowed}")
            if spec.kind is FactKind.TEXT:
                raise ValueError(f"Факт «{spec.label}» — текст, порог к нему не применим")


class ThresholdRule(RuleBase):
    type: Literal["threshold"]
    fact: str
    conditions: list[ThresholdCondition] = Field(min_length=1)
    on_missing: OnMissing | None = None

    @model_validator(mode="after")
    def _fact_and_literals_match(self) -> ThresholdRule:
        _check_fact_literals(self.fact, self.conditions)
        return self


class LlmCheckRule(RuleBase):
    type: Literal["llm_check"]
    group: str = Field(pattern=r"^[a-z_]{2,40}$")
    question: str = Field(min_length=10, max_length=1000)
    violation_if: str = Field(min_length=5, max_length=1000)
    ok_if: str = Field(min_length=5, max_length=1000)
    search_hints: list[str] = Field(min_length=1)
    absent_behavior: AbsentBehavior = AbsentBehavior.IGNORE
    absent_severity: Severity | None = None
    absent_message: str | None = None
    severity: Severity
    # «тревожные формулировки»: если такая есть в документе, ответ модели «в порядке» или «не
    # найдено» не принимается без человека — модель не может сама «простить» явный риск.
    # Регулярные выражения, как у `pattern`
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("red_flags")
    @classmethod
    def _valid_flags(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return check_patterns(values, timed=_timed(info))

    @model_validator(mode="after")
    def _absent_needs_text(self) -> LlmCheckRule:
        if self.absent_behavior is AbsentBehavior.REPORT and not (
            self.absent_severity and self.absent_message
        ):
            raise ValueError("При absent_behavior: report нужны absent_severity и absent_message")
        return self


class CrossDocumentRule(RuleBase):
    type: Literal["cross_document"]
    fact: str
    compare: list[DocType] = Field(min_length=2, max_length=2)
    severity: Severity

    @model_validator(mode="after")
    def _fact_known_and_distinct(self) -> CrossDocumentRule:
        if self.fact not in FACTS:
            raise ValueError(f"Неизвестный факт «{self.fact}»")
        if self.compare[0] == self.compare[1]:
            raise ValueError("Сравниваются два разных типа документов")
        return self


class InternalConsistencyRule(RuleBase):
    """Одно и то же условие указано в документе по-разному («5000 пачек» и «3000 пачек»)."""

    type: Literal["internal_consistency"]
    facts: list[str] = Field(min_length=1)
    severity: Severity

    @model_validator(mode="after")
    def _facts_known_and_numeric(self) -> InternalConsistencyRule:
        for key in self.facts:
            if key not in FACTS:
                raise ValueError(f"Неизвестный факт «{key}»")
            if FACTS[key].kind is not FactKind.NUMBER:
                raise ValueError(f"Сверять внутри документа можно только числа, а не «{key}»")
        return self


class SignalRule(RuleBase):
    """Сигнал оформления файла: скрытый текст и т. п. Находит программа, цвет — правило."""

    type: Literal["signal"]
    # hidden_text — текст, невидимый при просмотре; ai_instructions — обращение к ИИ-проверке
    signal: Literal["hidden_text", "ai_instructions"]
    severity: Severity


Rule = Annotated[
    PatternRule
    | ThresholdRule
    | LlmCheckRule
    | CrossDocumentRule
    | InternalConsistencyRule
    | SignalRule,
    Field(discriminator="type"),
]


class RulesetMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    title: str = Field(min_length=3, max_length=255)
    law: str
    version: int = Field(1, ge=1)


class RulesetFile(BaseModel):
    """Содержимое YAML-файла набора правил."""

    model_config = ConfigDict(extra="forbid")

    ruleset: RulesetMeta
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> RulesetFile:
        counts = Counter(r.id for r in self.rules)
        duplicates = sorted(code for code, n in counts.items() if n > 1)
        if duplicates:
            raise ValueError(f"Повторяются коды правил: {', '.join(duplicates)}")
        return self
