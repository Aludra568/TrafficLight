"""Тестовые записи БД: пакет, документ, разделы, фрагменты — с разумными значениями по умолчанию."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.orm import Session

from svetofor.core.config import get_settings
from svetofor.db.enums import DocType, DocumentStatus
from svetofor.db.models import Chunk, Document, Package, Section
from svetofor.rules.verify import normalize


def basis(index: int, dim: int | None = None) -> list[float]:
    """Единичный вектор: косинусная близость между разными базисными векторами равна нулю."""
    size = dim or get_settings().embed_dim
    vector = [0.0] * size
    vector[index % size] = 1.0
    return vector


def make_package(db: Session, title: str = "Тестовый пакет") -> Package:
    package = Package(
        title=title,
        source_filename=f"{title}.zip",
        size_bytes=1,
        sha256=hashlib.sha256(title.encode()).hexdigest(),
        storage_path=f"/data/packages/{uuid.uuid4()}",
    )
    db.add(package)
    db.flush()
    return package


def make_document(
    db: Session, package: Package, doc_type: DocType, filename: str | None = None
) -> Document:
    name = filename or f"{doc_type.value}.pdf"
    document = Document(
        package_id=package.id,
        filename=name,
        relative_path=name,
        sha256=hashlib.sha256(f"{package.id}{name}".encode()).hexdigest(),
        size_bytes=1,
        doc_type=doc_type,
        status=DocumentStatus.READY,
        page_count=1,
    )
    db.add(document)
    db.flush()
    return document


def make_section(db: Session, document: Document, path: str, number: str | None = None) -> Section:
    section = Section(
        document_id=document.id,
        number=number,
        title=path.rsplit(" › ", 1)[-1],
        path=path,
        level=1,
        page_from=1,
        page_to=1,
        char_start=0,
        char_end=0,
    )
    db.add(section)
    db.flush()
    return section


def make_chunk(
    db: Session,
    document: Document,
    text: str,
    order_index: int,
    *,
    section: Section | None = None,
    embedding: list[float] | None = None,
    **overrides: Any,
) -> Chunk:
    chunk = Chunk(
        document_id=document.id,
        section_id=section.id if section else None,
        order_index=order_index,
        section_path=section.path if section else document.filename,
        text=text,
        text_norm=normalize(text),
        page_from=1,
        page_to=1,
        char_start=0,
        char_end=len(text),
        token_count=max(1, round(len(text) / 3.5)),
        embedding=embedding,
        **overrides,
    )
    db.add(chunk)
    db.flush()
    return chunk
