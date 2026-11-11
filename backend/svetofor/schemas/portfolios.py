"""Контракты портфеля и обработки замечаний."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from svetofor.db.enums import Light

Handling = Literal["new", "in_progress", "resolved", "not_applicable"]


class HandlingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Handling
    version: int = Field(ge=0)


class HandlingOut(BaseModel):
    status: Handling
    version: int
    updated_at: datetime | None = None


class PortfolioItemOut(BaseModel):
    id: uuid.UUID
    position: int
    source_type: str
    source_name: str
    check_id: uuid.UUID | None = None
    status: Literal["waiting", "running", "done", "failed"]
    light: Light | None = None
    progress: int = 0
    error: str | None = None
    findings: int = 0


class RepeatedRisk(BaseModel):
    rule_code: str
    title: str
    procurements: int


class PortfolioFinding(BaseModel):
    id: uuid.UUID
    check_id: uuid.UUID
    source_name: str
    rule_code: str
    title: str
    severity: str
    category: str
    category_title: str = ""
    quote: str | None = None
    document_id: uuid.UUID | None = None
    page: int | None = None
    handling_status: Handling = "new"


class PortfolioOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    light: Light
    complete: bool
    total: int
    done: int
    running: int
    waiting: int
    failed: int
    distribution: dict[str, int]
    items: list[PortfolioItemOut]
    repeated_risks: list[RepeatedRisk]
    findings: list[PortfolioFinding]


class PortfolioBrief(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    total: int


class PortfolioList(BaseModel):
    items: list[PortfolioBrief]
    total: int


class PortfolioCreated(BaseModel):
    id: uuid.UUID
    dispatch_error: str | None = None
