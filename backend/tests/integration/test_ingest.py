"""Приём и подготовка пакета на PostgreSQL: распаковка, документы, страницы, разделы, фрагменты."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import DocType, DocTypeSource, DocumentStatus, Stage, StageStatus
from svetofor.db.models import Check, CheckStage, Chunk, Document, Page, Section
from svetofor.pipeline.doctype import DocTypeGuess
from svetofor.pipeline.ingest import (
    CheckFailedError,
    IngestConfig,
    create_check,
    new_check,
    prepare_package,
    process_document,
)
from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup

pytestmark = pytest.mark.pg

CONTRACT = """
!! ПРОЕКТ КОНТРАКТА
ГКУ «Заказчик», именуемое в дальнейшем «Заказчик», заключили настоящий Контракт:
**1. ПРЕДМЕТ КОНТРАКТА**
1.1. Поставщик обязуется поставить бумагу для офисной техники.
**2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ**
2.1. Оплата производится в течение 7 рабочих дней с даты подписания документа о приёмке.
===
**3. ОТВЕТСТВЕННОСТЬ СТОРОН**
3.1. Пеня начисляется за каждый день просрочки.
"""
SPEC = """
!! ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ
**1. Требования к товару**
1.1. Бумага формата А4, плотность 80 г/м2.
"""
NEUTRAL = """
**1. Общие сведения**
1.1. Бумага формата А4, плотность 80 г/м2, количество 500 пачек.
"""


def pdf_bytes(tmp_path: Path, markup: str, name: str) -> bytes:
    return build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / name).read_bytes()


def upload_zip(tmp_path: Path, extra: list[tuple[str, bytes]] | None = None) -> Path:
    archive = tmp_path / "upload.bin"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Проект контракта.pdf", pdf_bytes(tmp_path, CONTRACT, "c.pdf"))
        zf.writestr("ТЗ/Описание объекта закупки.pdf", pdf_bytes(tmp_path, SPEC, "s.pdf"))
        zf.writestr("Битый.pdf", b"%PDF-1.4 broken")
        zf.writestr("readme.exe", b"MZ")
        zf.writestr("Пустой.docx", b"")
        for name, data in extra or []:
            zf.writestr(name, data)
    return archive


@pytest.fixture
def config(tmp_path: Path) -> IngestConfig:
    return IngestConfig(packages_dir=tmp_path / "packages")


def start(
    factory: sessionmaker[Session],
    upload: Path,
    config: IngestConfig,
    name: str = "Закупка 0373.zip",
) -> Check:
    with factory() as db:
        check = create_check(
            db, upload=upload, original_name=name, config=config, ruleset_version_id=None
        )
        db.commit()
        return check


def by_name(db: Session) -> dict[str, Document]:
    return {d.filename: d for d in db.scalars(select(Document))}


def test_package_documents_and_skips(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    check = start(session_factory, upload_zip(tmp_path), config)
    ids = prepare_package(session_factory, check.id, config)
    assert len(ids) == 3
    with session_factory() as db:
        docs = by_name(db)
        assert docs["readme.exe"].status is DocumentStatus.SKIPPED
        assert docs["readme.exe"].error == "формат .exe не поддерживается"
        assert docs["Пустой.docx"].error == "пустой файл"
        assert (
            docs["Описание объекта закупки.pdf"].relative_path == "ТЗ/Описание объекта закупки.pdf"
        )
        stage = db.scalar(
            select(CheckStage).where(
                CheckStage.check_id == check.id, CheckStage.stage == Stage.UNPACK
            )
        )
        assert stage is not None and stage.status is StageStatus.DONE
        assert stage.detail == "Документов: 3, пропущено: 2"
        assert db.get(Check, check.id).timings.get("unpack") is not None  # type: ignore[union-attr]


def test_documents_processed_end_to_end(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    check = start(session_factory, upload_zip(tmp_path), config)
    statuses = [
        process_document(session_factory, i, config)
        for i in prepare_package(session_factory, check.id, config)
    ]
    assert sorted(statuses) == sorted(
        [DocumentStatus.READY, DocumentStatus.READY, DocumentStatus.FAILED]
    )
    with session_factory() as db:
        docs = by_name(db)
        contract, spec = docs["Проект контракта.pdf"], docs["Описание объекта закупки.pdf"]
        assert (contract.doc_type, contract.doc_type_source) == (
            DocType.CONTRACT,
            DocTypeSource.RULE,
        )
        assert spec.doc_type is DocType.SPEC
        assert docs["Битый.pdf"].status is DocumentStatus.FAILED and "повреждён" in (
            docs["Битый.pdf"].error or ""
        )
        assert contract.page_count == 2 and contract.error is None
        assert Path(contract.pdf_path or "").is_file()
        pages = list(
            db.scalars(select(Page).where(Page.document_id == contract.id).order_by(Page.number))
        )
        text = "\n".join(p.text for p in pages)
        chunks = list(
            db.scalars(
                select(Chunk).where(Chunk.document_id == contract.id).order_by(Chunk.order_index)
            )
        )
        assert chunks and all(text[c.char_start : c.char_end] == c.text for c in chunks)
        paths = set(db.scalars(select(Section.path).where(Section.document_id == contract.id)))
        assert "Проект контракта › 2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ › 2.1" in paths
        penalty = next(c for c in chunks if "Пеня" in c.text)
        assert penalty.page_from == 2 and penalty.section_id is not None


def test_processing_twice_does_not_duplicate(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    check = start(session_factory, upload_zip(tmp_path), config)
    contract_id = prepare_package(session_factory, check.id, config)[0]
    process_document(session_factory, contract_id, config)
    with session_factory() as db:
        db.get(Document, contract_id).status = DocumentStatus.PENDING  # type: ignore[union-attr]
        db.commit()
    process_document(session_factory, contract_id, config)
    with session_factory() as db:
        pages = db.scalar(
            select(func.count()).select_from(Page).where(Page.document_id == contract_id)
        )
        doc = db.get(Document, contract_id)
        assert pages == doc.page_count  # type: ignore[union-attr]


def test_rerun_reuses_prepared_documents(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    check = start(session_factory, upload_zip(tmp_path), config)
    first = prepare_package(session_factory, check.id, config)
    with session_factory() as db:
        again = new_check(db, db.get(Check, check.id).package, None)  # type: ignore[union-attr]
        db.commit()
    assert prepare_package(session_factory, again.id, config) == first
    with session_factory() as db:
        stage = db.scalar(
            select(CheckStage).where(
                CheckStage.check_id == again.id, CheckStage.stage == Stage.UNPACK
            )
        )
        assert stage is not None and stage.status is StageStatus.SKIPPED


def test_broken_archive_fails_check(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not a zip at all")
    check = start(session_factory, bad, config, name="Закупка.zip")
    with pytest.raises(CheckFailedError, match="повреждён"):
        prepare_package(session_factory, check.id, config)
    with session_factory() as db:
        stage = db.scalar(
            select(CheckStage).where(
                CheckStage.check_id == check.id, CheckStage.stage == Stage.UNPACK
            )
        )
        assert stage is not None and stage.status is StageStatus.FAILED


def test_uncertain_type_asks_classifier(
    session_factory: sessionmaker[Session], tmp_path: Path, config: IngestConfig
) -> None:
    upload = tmp_path / "upload.pdf"
    upload.write_bytes(pdf_bytes(tmp_path, NEUTRAL, "n.pdf"))
    check = start(session_factory, upload, config, name="Приложение 2.pdf")
    calls: list[str] = []

    def classify(filename: str, text: str) -> DocTypeGuess:
        calls.append(filename)
        return DocTypeGuess(DocType.SPEC, 0.8, "по ответу модели")

    (doc_id,) = prepare_package(session_factory, check.id, config)
    process_document(session_factory, doc_id, config, classify)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        assert doc is not None and (doc.doc_type, doc.doc_type_source) == (
            DocType.SPEC,
            DocTypeSource.LLM,
        )
    assert calls == ["Приложение 2.pdf"]
