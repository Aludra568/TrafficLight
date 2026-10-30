"""Схемы API правил, наборов, песочницы и качества."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from svetofor.db.enums import DocType, RuleStatus


class RuleListItem(BaseModel):
    code: str
    title: str
    category: str
    category_title: str
    type: str
    severity: str | None = Field(description="Самый серьёзный цвет, который может дать правило")
    status: RuleStatus
    law: list[str]
    applies_to: list[DocType]
    enabled: bool
    version: int
    updated_at: datetime
    verified_by: str | None
    edition_checked: date | None = Field(description="Самая старая дата сверки нормы")
    stale: bool = Field(description="Норму пора перепроверить (LEGAL_STALE_DAYS)")
    triggers_30d: int
    precision: float | None = Field(description="Доля отметок «верно» среди отметок по правилу")
    feedback_count: int


class RuleVersionOut(BaseModel):
    version: int
    created_at: datetime
    status: RuleStatus
    change_note: str | None
    author: str | None


class RuleDetail(BaseModel):
    code: str
    body: dict[str, Any]
    current_version: int
    versions: list[RuleVersionOut]


class RuleIn(BaseModel):
    rule: dict[str, Any] = Field(description="Описание правила, как в YAML")
    change_note: str | None = Field(None, max_length=500)


class RuleSaved(BaseModel):
    code: str
    version: int
    ruleset_version: int | None = Field(description="Версия набора, в которую правило вошло")


class VerifyIn(BaseModel):
    verified_by: str = Field(min_length=3, max_length=255, description="ФИО и роль юриста")
    verified_at: date | None = None


class RulesetOut(BaseModel):
    code: str
    title: str
    law: str
    version: int
    rules: int
    published_at: datetime


class SandboxIn(BaseModel):
    rule_code: str | None = Field(None, description="Проверить сохранённое правило")
    rule: dict[str, Any] | None = Field(None, description="…или черновик из редактора")
    text: str = Field(min_length=20, max_length=30_000)
    doc_type: DocType | None = None


class SandboxTask(BaseModel):
    task_id: str


class SandboxResult(BaseModel):
    status: Literal["pending", "done", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None


class RuleFeedbackStats(BaseModel):
    rule_code: str
    correct: int
    false_positive: int
    missed: int
    precision: float | None


class QualityOut(BaseModel):
    eval: dict[str, Any] | None = Field(description="Последний прогон оценки на размеченном наборе")
    feedback: list[RuleFeedbackStats]
    llm: dict[str, Any]
    quotes: dict[str, int]
    models: dict[str, Any]
