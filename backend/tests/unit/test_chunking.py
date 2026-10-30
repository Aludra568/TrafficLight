"""Фрагменты: точные смещения, полное покрытие текста, границы разделов, нарезка длинных пунктов."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from svetofor.pipeline.chunking import ChunkDraft, estimate_tokens, make_chunks
from svetofor.pipeline.extract import ExtractedDocument, extract_document
from svetofor.pipeline.structure import build_structure
from svetofor.rules.verify import normalize
from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup
from tests.unit.test_structure import CONTRACT


def chunks_of(tmp_path: Path, markup: str, **kw: int) -> tuple[ExtractedDocument, list[ChunkDraft]]:
    pdf = build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / "doc.pdf")
    doc = extract_document(pdf)
    return doc, make_chunks(doc, build_structure(doc, "Проект контракта"), **kw)


def assert_exact_and_complete(doc: ExtractedDocument, chunks: list[ChunkDraft]) -> None:
    covered = [False] * len(doc.text)
    for chunk in chunks:
        assert doc.text[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.text == chunk.text.strip()
        assert chunk.text_norm == normalize(chunk.text)
        assert chunk.page_from <= chunk.page_to
        for i in range(chunk.char_start, chunk.char_end):
            covered[i] = True
    lost = "".join(ch for ch, ok in zip(doc.text, covered, strict=True) if not ok and ch.strip())
    assert lost == "", f"текст не попал ни в один фрагмент: {lost!r}"


def test_contract_chunks_exact_and_complete(tmp_path: Path) -> None:
    doc, chunks = chunks_of(tmp_path, CONTRACT)
    assert_exact_and_complete(doc, chunks)
    assert [c.order for c in chunks] == list(range(len(chunks)))


def test_heading_goes_with_its_clauses(tmp_path: Path) -> None:
    _, chunks = chunks_of(tmp_path, CONTRACT)
    first = next(c for c in chunks if "1.1. Поставщик обязуется" in c.text)
    assert first.text.startswith("1. ПРЕДМЕТ КОНТРАКТА")
    assert first.section_path == "Проект контракта › 1. ПРЕДМЕТ КОНТРАКТА"
    preamble = next(c for c in chunks if "именуемое в дальнейшем" in c.text)
    assert "ПРЕДМЕТ КОНТРАКТА" not in preamble.text  # вводная часть отдельно
    assert preamble.section_path == "Проект контракта › Вводная часть"


def test_chunks_do_not_cross_sections(tmp_path: Path) -> None:
    _, chunks = chunks_of(tmp_path, CONTRACT)
    for chunk in chunks:
        top_headings = [
            h for h in ("1. ПРЕДМЕТ", "2. ЦЕНА", "3. ПОРЯДОК", "4. ОТВЕТ") if h in chunk.text
        ]
        assert len(top_headings) <= 1, chunk.text


def test_small_clauses_of_one_section_merged(tmp_path: Path) -> None:
    _, chunks = chunks_of(tmp_path, CONTRACT, max_tokens=700, min_tokens=120)
    price = next(c for c in chunks if "2.2. Оплата" in c.text)
    assert "2.1. Цена Контракта" in price.text  # мелкие пункты одного раздела вместе
    assert price.section_path == "Проект контракта › 2. ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ"


def test_no_merging_when_min_tokens_is_zero(tmp_path: Path) -> None:
    _, chunks = chunks_of(tmp_path, CONTRACT, max_tokens=700, min_tokens=0)
    clause = next(c for c in chunks if "4.1. Пеня" in c.text)
    assert clause.text.startswith("4.1.") and "4.2." not in clause.text
    assert clause.section_path.endswith("› 4.1")


def test_long_clause_split_with_overlap(tmp_path: Path) -> None:
    sentences = [
        f"Предложение номер {i} описывает требование к товару подробно." for i in range(30)
    ]
    markup = "**1. ТРЕБОВАНИЯ**\n1.1. " + "\n".join(sentences)
    doc, chunks = chunks_of(tmp_path, markup, max_tokens=80, min_tokens=0)
    assert_exact_and_complete(doc, chunks)
    long_parts = [c for c in chunks if "Предложение номер" in c.text]
    assert len(long_parts) >= 5
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= 80
    for a, b in itertools.pairwise(long_parts):
        assert b.char_start < a.char_end  # перекрытие в один абзац
        assert b.char_start > a.char_start


def test_embedding_text_has_section_path(tmp_path: Path) -> None:
    _, chunks = chunks_of(tmp_path, CONTRACT)
    chunk = next(c for c in chunks if "Пеня начисляется" in c.text)
    assert chunk.embedding_text.startswith("Проект контракта › 4. ОТВЕТСТВЕННОСТЬ СТОРОН: ")


@pytest.mark.parametrize(("text", "tokens"), [("", 0), ("   ", 0), ("слово", 2), ("а" * 35, 10)])
def test_estimate_tokens(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens
