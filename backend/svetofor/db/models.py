"""Модели БД (SQLAlchemy 2).

Соглашения:
- первичные ключи — UUIDv7 (сортируются по времени);
- удаление пакета каскадно удаляет всё производное (ON DELETE CASCADE на уровне БД);
- перечисления хранятся строками с CHECK-ограничением (без нативных enum PostgreSQL).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from svetofor.core.config import get_settings
from svetofor.core.ids import uuid7
from svetofor.db.enums import (
    CheckStatus,
    DocType,
    DocTypeSource,
    DocumentStatus,
    FeedbackVerdict,
    FindingKind,
    FindingStatus,
    Light,
    Role,
    RuleOutcome,
    RuleStatus,
    Severity,
    StageStatus,
)

EMBED_DIM = get_settings().embed_dim


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,  # без этого CHECK не создаётся и в БД можно записать любую строку
        values_callable=lambda e: [m.value for m in e],
        length=32,
        validate_strings=True,
        name=enum_cls.__name__.lower(),
    )


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}  # noqa: RUF012 — API SQLAlchemy


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def fk(target: str, *, ondelete: str = "CASCADE", nullable: bool = False, **kw: Any) -> Any:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=True,
        **kw,
    )


# ─────────────────────────── Пользователи ───────────────────────────


class User(IdMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[Role] = mapped_column(str_enum(Role), nullable=False, default=Role.SPECIALIST)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ─────────────────────────── Пакеты и документы ───────────────────────────


class Package(IdMixin, Base):
    __tablename__ = "packages"

    owner_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    documents: Mapped[list[Document]] = relationship(
        back_populates="package", cascade="all, delete-orphan", passive_deletes=True
    )
    checks: Mapped[list[Check]] = relationship(
        back_populates="package", cascade="all, delete-orphan", passive_deletes=True
    )


class Document(IdMixin, Base):
    __tablename__ = "documents"

    package_id: Mapped[uuid.UUID] = fk("packages.id")
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    doc_type: Mapped[DocType] = mapped_column(
        str_enum(DocType), nullable=False, default=DocType.OTHER
    )
    doc_type_source: Mapped[DocTypeSource] = mapped_column(
        str_enum(DocTypeSource), nullable=False, default=DocTypeSource.RULE
    )
    doc_type_confidence: Mapped[float | None] = mapped_column(Float)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus), nullable=False, default=DocumentStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text)
    # сколько раз документ брался в обработку: падение воркера не должно зациклить очередь
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # сигналы оформления: {"hidden_text": [{page, char_start, char_end, text}], "revisions": N}
    signals: Mapped[dict[str, Any] | None] = mapped_column()

    package: Mapped[Package] = relationship(back_populates="documents")
    pages: Mapped[list[Page]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Page.number",
    )


class Page(IdMixin, Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "number", name="uq_pages_document_number"),)

    document_id: Mapped[uuid.UUID] = fk("documents.id")
    number: Mapped[int] = mapped_column(Integer, nullable=False)  # с 1
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_offset_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    height: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    document: Mapped[Document] = relationship(back_populates="pages")


class Section(IdMixin, Base):
    __tablename__ = "sections"

    document_id: Mapped[uuid.UUID] = fk("documents.id")
    parent_id: Mapped[uuid.UUID | None] = fk("sections.id", nullable=True)
    number: Mapped[str | None] = mapped_column(String(64))  # «7.3»
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    path: Mapped[str] = mapped_column(Text, nullable=False)  # «Проект контракта › 7. … › 7.3»
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_from: Mapped[int] = mapped_column(Integer, nullable=False)
    page_to: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)


class Chunk(IdMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = fk("documents.id")
    section_id: Mapped[uuid.UUID | None] = fk("sections.id", ondelete="SET NULL", nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_norm: Mapped[str] = mapped_column(Text, nullable=False)
    page_from: Mapped[int] = mapped_column(Integer, nullable=False)
    page_to: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(text_norm, ''))", persisted=True)
    )


# ─────────────────────────── Правила ───────────────────────────


class Rule(IdMixin, Base):
    __tablename__ = "rules"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # «PAY-01»
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "rule_versions.id", ondelete="SET NULL", use_alter=True, name="fk_rules_current_version"
        ),
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    versions: Mapped[list[RuleVersion]] = relationship(
        back_populates="rule", foreign_keys="RuleVersion.rule_id", order_by="RuleVersion.version"
    )
    current_version: Mapped[RuleVersion | None] = relationship(foreign_keys=[current_version_id])


class RuleVersion(IdMixin, Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_rule_versions_rule_version"),)

    rule_id: Mapped[uuid.UUID] = fk("rules.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(nullable=False)  # полное описание правила
    status: Mapped[RuleStatus] = mapped_column(
        str_enum(RuleStatus), nullable=False, default=RuleStatus.DRAFT
    )
    verified_by: Mapped[str | None] = mapped_column(String(255))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text)

    rule: Mapped[Rule] = relationship(back_populates="versions", foreign_keys=[rule_id])


class Ruleset(IdMixin, Base):
    __tablename__ = "rulesets"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    law: Mapped[str] = mapped_column(String(32), nullable=False)


class RulesetVersion(IdMixin, Base):
    __tablename__ = "ruleset_versions"
    __table_args__ = (UniqueConstraint("ruleset_id", "version", name="uq_ruleset_versions"),)

    ruleset_id: Mapped[uuid.UUID] = fk("rulesets.id")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)

    ruleset: Mapped[Ruleset] = relationship()


# ─────────────────────────── Проверки ───────────────────────────


class Check(IdMixin, Base):
    __tablename__ = "checks"

    package_id: Mapped[uuid.UUID] = fk("packages.id")
    ruleset_version_id: Mapped[uuid.UUID | None] = fk(
        "ruleset_versions.id", ondelete="SET NULL", nullable=True
    )
    status: Mapped[CheckStatus] = mapped_column(
        str_enum(CheckStatus), nullable=False, default=CheckStatus.QUEUED
    )
    law_detected: Mapped[str | None] = mapped_column(String(32))
    light: Mapped[Light | None] = mapped_column(str_enum(Light))
    risk_index: Mapped[float | None] = mapped_column(Float)
    llm_model_main: Mapped[str | None] = mapped_column(String(255))
    llm_model_fast: Mapped[str | None] = mapped_column(String(255))
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    timings: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    counters: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # версии OCR/LibreOffice/разборщика, модель эмбеддингов, бюджеты и пороги (воспроизводимость)
    pipeline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    package: Mapped[Package] = relationship(back_populates="checks")
    stages: Mapped[list[CheckStage]] = relationship(
        back_populates="check",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CheckStage.order_index",
    )


class CheckStage(IdMixin, Base):
    __tablename__ = "check_stages"
    __table_args__ = (UniqueConstraint("check_id", "stage", name="uq_check_stages"),)

    check_id: Mapped[uuid.UUID] = fk("checks.id")
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[StageStatus] = mapped_column(
        str_enum(StageStatus), nullable=False, default=StageStatus.PENDING
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    check: Mapped[Check] = relationship(back_populates="stages")


class Fact(IdMixin, Base):
    __tablename__ = "facts"

    check_id: Mapped[uuid.UUID] = fk("checks.id")
    document_id: Mapped[uuid.UUID | None] = fk("documents.id", nullable=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    chunk_id: Mapped[uuid.UUID | None] = fk("chunks.id", ondelete="SET NULL", nullable=True)
    page_from: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)  # цитата в тексте документа
    char_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # каким каналом найден: llm — модель, rules — грамматика, both — оба согласны
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="llm", server_default="llm"
    )
    conflict: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # значение другого канала
    # другие значения поля в документе (этапы оплаты, расхождение цифр и прописи)
    candidates: Mapped[list[Any] | None] = mapped_column(JSONB)


class Finding(IdMixin, Base):
    __tablename__ = "findings"

    check_id: Mapped[uuid.UUID] = fk("checks.id")
    document_id: Mapped[uuid.UUID | None] = fk("documents.id", nullable=True)
    rule_version_id: Mapped[uuid.UUID | None] = fk(
        "rule_versions.id", ondelete="SET NULL", nullable=True
    )
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[Severity] = mapped_column(str_enum(Severity), nullable=False)
    kind: Mapped[FindingKind] = mapped_column(str_enum(FindingKind), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    quote_score: Mapped[float | None] = mapped_column(Float)
    section_path: Mapped[str | None] = mapped_column(Text)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    highlights: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    related: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[FindingStatus] = mapped_column(
        str_enum(FindingStatus), nullable=False, default=FindingStatus.SHOWN
    )


class RuleResult(IdMixin, Base):
    __tablename__ = "rule_results"

    check_id: Mapped[uuid.UUID] = fk("checks.id")
    rule_version_id: Mapped[uuid.UUID | None] = fk(
        "rule_versions.id", ondelete="SET NULL", nullable=True
    )
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[RuleOutcome] = mapped_column(str_enum(RuleOutcome), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # что видела модель (фрагменты) и чем подтверждён исход «в порядке» (цитата)
    details: Mapped[dict[str, Any] | None] = mapped_column()


class Feedback(IdMixin, Base):
    __tablename__ = "feedback"

    finding_id: Mapped[uuid.UUID | None] = fk("findings.id", ondelete="SET NULL", nullable=True)
    check_id: Mapped[uuid.UUID] = fk("checks.id")
    user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    verdict: Mapped[FeedbackVerdict] = mapped_column(str_enum(FeedbackVerdict), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)


# ─────────────────────────── Служебные ───────────────────────────


class LLMCacheEntry(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LLMCall(IdMixin, Base):
    __tablename__ = "llm_calls"

    check_id: Mapped[uuid.UUID | None] = fk("checks.id", nullable=True)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)


class EvalRun(IdMixin, Base):
    __tablename__ = "eval_runs"

    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    model_main: Mapped[str | None] = mapped_column(String(255))
    ruleset_version_id: Mapped[uuid.UUID | None] = fk(
        "ruleset_versions.id", ondelete="SET NULL", nullable=True
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    report_md: Mapped[str | None] = mapped_column(Text)


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"

    user_id: Mapped[uuid.UUID | None] = fk("users.id", ondelete="SET NULL", nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
