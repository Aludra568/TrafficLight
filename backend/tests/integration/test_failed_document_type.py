"""Документ, который не открылся, получает тип по имени файла.

Иначе в отчёте у непрочитанного «Проект контракта.docx» стояло «Похоже на „Другой документ“.
Верно?» — догадка о файле, которого никто не читал.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import DocType, DocTypeSource, DocumentStatus
from svetofor.db.models import Check, Document
from tests.integration.test_analysis import deps
from tests.integration.test_redteam_pipeline import _run

pytestmark = pytest.mark.pg


def test_unreadable_file_is_typed_by_its_name(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    archive = tmp_path / "Закупка.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Проект контракта.docx", b"not a docx")  # не откроется ни с LibreOffice, ни без
        zf.writestr("scan001.docx", b"not a docx")
    check_id = _run(session_factory, tmp_path, archive, deps(None))
    with session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None
        documents = {
            d.filename: d
            for d in db.scalars(select(Document).where(Document.package_id == check.package_id))
        }
    contract = documents["Проект контракта.docx"]
    assert contract.status is DocumentStatus.FAILED
    assert contract.doc_type is DocType.CONTRACT
    assert contract.doc_type_source is DocTypeSource.RULE
    # по имени ничего не понять — тип остаётся «прочее», а не выдуманный
    assert documents["scan001.docx"].doc_type is DocType.OTHER
