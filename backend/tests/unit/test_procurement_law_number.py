"""Определение закона закупки по реестровому номеру извещения."""

from __future__ import annotations

from typing import Any

from svetofor.db.enums import DocType
from svetofor.rules.facts import DocumentFacts, FactValue, procurement_law


def _fact(key: str, value: Any, doc_type: DocType = DocType.NOTICE) -> FactValue:
    text = str(value)
    return FactValue(
        key=key,
        value=value,
        unit=None,
        raw=text,
        quote=text,
        chunk_id="c1",
        document_id="doc-1",
        doc_type=doc_type,
        page_from=1,
        char_start=0,
        char_end=len(text),
        score=100.0,
    )


def _document(
    document_id: str = "doc-1",
    *,
    law: str | None = None,
    procurement_number: str | None = None,
    doc_type: DocType = DocType.NOTICE,
) -> DocumentFacts:
    facts: dict[str, FactValue] = {}
    if law is not None:
        facts["law"] = _fact("law", law, doc_type)
    if procurement_number is not None:
        facts["procurement_number"] = _fact("procurement_number", procurement_number, doc_type)
    return DocumentFacts(document_id, doc_type, facts)


def test_law_undefined_and_19_digit_number_resolves_to_44fz() -> None:
    doc = _document(procurement_number="0373100012326000123")
    assert procurement_law([doc]) == "44-ФЗ"

    doc_formatted = _document(procurement_number="№ 0373 1000 1232 6000 123")
    assert procurement_law([doc_formatted]) == "44-ФЗ"

    doc1 = _document("doc-1", procurement_number="0373100012326000123")
    doc2 = _document("doc-2", procurement_number="0373100012326000123", doc_type=DocType.CONTRACT)
    assert procurement_law([doc1, doc2]) == "44-ФЗ"

    doc3 = _document("doc-3", doc_type=DocType.SPEC)
    assert procurement_law([doc1, doc3]) == "44-ФЗ"

    # Разные значения закона в документах — закон по факту law не определён
    doc_conflict1 = _document("doc-4", law="44-ФЗ", procurement_number="0373100012326000123")
    doc_conflict2 = _document("doc-5", law="223-ФЗ", procurement_number="0373100012326000123")
    assert procurement_law([doc_conflict1, doc_conflict2]) == "44-ФЗ"


def test_law_undefined_and_11_digit_number_resolves_to_223fz() -> None:
    doc = _document(procurement_number="32301234567")
    assert procurement_law([doc]) == "223-ФЗ"

    doc_formatted = _document(procurement_number="№ 3230 1234 567")
    assert procurement_law([doc_formatted]) == "223-ФЗ"

    doc1 = _document("doc-1", procurement_number="32301234567")
    doc2 = _document("doc-2", procurement_number="32301234567", doc_type=DocType.CONTRACT)
    assert procurement_law([doc1, doc2]) == "223-ФЗ"


def test_law_undefined_and_different_lengths_yields_none() -> None:
    doc_19 = _document("doc-1", procurement_number="0373100012326000123")
    doc_11 = _document("doc-2", procurement_number="32301234567")
    assert procurement_law([doc_19, doc_11]) is None

    doc_other = _document(procurement_number="123456789012345")
    assert procurement_law([doc_other]) is None

    doc_short = _document(procurement_number="1234567890")
    assert procurement_law([doc_short]) is None

    doc_empty = _document()
    assert procurement_law([doc_empty]) is None


def test_law_defined_by_law_ignores_number() -> None:
    doc_44 = _document(law="44-ФЗ", procurement_number="32301234567")
    assert procurement_law([doc_44]) == "44-ФЗ"

    doc_223 = _document(law="223-ФЗ", procurement_number="0373100012326000123")
    assert procurement_law([doc_223]) == "223-ФЗ"

    doc_commercial = _document(law="commercial", procurement_number="0373100012326000123")
    assert procurement_law([doc_commercial]) == "commercial"

    doc1 = _document("doc-1", law="44-ФЗ", procurement_number="0373100012326000123")
    doc2 = _document("doc-2", law="44-ФЗ", procurement_number="32301234567")
    assert procurement_law([doc1, doc2]) == "44-ФЗ"
