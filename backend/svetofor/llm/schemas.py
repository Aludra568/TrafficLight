"""Схемы ответов модели. Модель возвращает только вердикт, цитату
и пояснение — цвет задаёт правило.

Порядок полей — порядок рассуждения (Schema-Guided Reasoning): модель пишет ответ слева
направо, поэтому сначала место и цитата, потом пояснение и только потом вывод. Иначе вывод
делается до того, как найдено основание, и цитату подбирают под готовый вывод.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from svetofor.db.enums import DocType


class RuleVerdict(BaseModel):
    rule_id: str
    chunk_id: str | None = None
    quote: str | None = Field(None, max_length=600)
    explanation: str = Field("", max_length=600)
    verdict: Literal["violation", "ok", "not_found"]


class GroupResult(BaseModel):
    results: list[RuleVerdict]


class ReviewItem(BaseModel):  # второе мнение: рассуждение — до вывода
    item_id: str
    reasoning: str = Field("", max_length=400)
    decision: Literal["confirmed", "rejected", "unsure"]


class ReviewResult(BaseModel):
    reviews: list[ReviewItem]


class SweepVerdict(BaseModel):  # поштучная проверка: правила и довод — до вывода
    unit_id: str
    rule_ids: list[str] = Field(default_factory=list, max_length=10)
    reasoning: str = Field("", max_length=300)
    verdict: Literal["none", "violation", "unsure"]


class SweepResult(BaseModel):
    units: list[SweepVerdict]


class ExtractedFact(BaseModel):
    key: str
    chunk_id: str | None = None
    quote: str | None = Field(None, max_length=600)
    raw: str | None = Field(None, max_length=600)
    value: bool | int | float | str | None = None
    unit: str | None = Field(None, max_length=32)  # «working»/«calendar» для сроков, «шт.»…


class FactsResult(BaseModel):
    facts: list[ExtractedFact]


class DocTypeAnswer(BaseModel):
    doc_type: DocType
    confidence: float = Field(ge=0, le=1)
