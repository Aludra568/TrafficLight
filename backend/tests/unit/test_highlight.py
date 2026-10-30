"""Подсветка: прямоугольники покрывают ровно цитату, повторы фразы не подсвечиваются."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from svetofor.pipeline.extract import ExtractedDocument, extract_document
from svetofor.pipeline.highlight import Highlight, Highlighter
from tests.support.pdfgen import MARGIN_X, PdfSpec, build_pdf, pages_from_markup

MARKUP = """
**1. СРОКИ**
1.1. Поставка осуществляется в течение 10 рабочих дней с даты заявки.
1.2. Приёмка осуществляется в течение 10 рабочих дней с даты поставки,
а оплата производится в течение 10 рабочих дней с даты приёмки.
===
1.3. Гарантийный срок составляет 12 месяцев
с даты подписания документа о приёмке.
"""


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, ExtractedDocument]:
    pdf = build_pdf(PdfSpec(pages=pages_from_markup(MARKUP)), tmp_path / "doc.pdf")
    return pdf, extract_document(pdf)


def text_under(pdf: Path, highlights: list[Highlight]) -> str:
    """Текст, который реально оказался под прямоугольниками."""
    parts = []
    with pymupdf.open(pdf) as document:
        for h in highlights:
            page = document[h.page - 1]
            for x0, y0, x1, y1 in h.rects:
                # чуть уже: get_textbox берёт и символы, которых прямоугольник касается краем
                inner = pymupdf.Rect(x0 + 0.5, y0, x1 - 0.5, y1)
                parts.append(page.get_textbox(inner).strip())
    return " ".join(parts)


def locate(pdf: Path, doc: ExtractedDocument, quote: str, occurrence: int = 0) -> list[Highlight]:
    start = -1
    for _ in range(occurrence + 1):
        start = doc.text.index(quote, start + 1)
    with Highlighter(pdf, doc) as highlighter:
        return highlighter.locate(start, start + len(quote))


def test_whole_line(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    line = next(ln for ln in doc.lines if ln.text.startswith("1.1."))
    highlights = locate(pdf, doc, line.text)
    assert highlights == [Highlight(1, (tuple(round(v, 2) for v in line.bbox),))]


def test_part_of_line_and_repeated_phrase(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    phrase = "в течение 10 рабочих дней с даты приёмки"
    highlights = locate(pdf, doc, phrase)
    assert len(highlights) == 1 and len(highlights[0].rects) == 1
    assert text_under(pdf, highlights) == phrase
    # та же фраза встречается в 1.1 и 1.2 — они не подсвечены
    rect = highlights[0].rects[0]
    line = next(ln for ln in doc.lines if ln.text.startswith("а оплата"))
    assert line.bbox[1] - 1 <= rect[1] and rect[3] <= line.bbox[3] + 1
    assert rect[0] > MARGIN_X + 10  # не с начала строки


def test_second_occurrence_on_same_line_is_chosen(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    first = locate(pdf, doc, "в течение 10 рабочих дней", occurrence=0)
    second = locate(pdf, doc, "в течение 10 рабочих дней", occurrence=1)
    assert first[0].rects[0][1] < second[0].rects[0][1]  # разные строки — разные места


def test_repeat_within_one_line(tmp_path: Path) -> None:
    markup = "1.1. Срок 5 дней, затем срок 5 дней, и ещё срок 5 дней."
    pdf = build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / "doc.pdf")
    doc = extract_document(pdf)
    rects = [locate(pdf, doc, "срок 5 дней", occurrence=k)[0].rects[0] for k in range(2)]
    assert rects[0][0] < rects[1][0]  # второе вхождение правее первого
    assert all(text_under(pdf, [Highlight(1, (r,))]) == "срок 5 дней" for r in rects)


def test_quote_across_lines(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    quote = "с даты поставки,\nа оплата производится"
    highlights = locate(pdf, doc, quote)
    assert len(highlights) == 1 and len(highlights[0].rects) == 2
    assert text_under(pdf, highlights) == "с даты поставки, а оплата производится"


def test_quote_across_pages(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    start = doc.text.index("с даты приёмки.")
    end = doc.text.index("12 месяцев") + len("12 месяцев")
    with Highlighter(pdf, doc) as highlighter:
        highlights = highlighter.locate(start, end)
    assert [h.page for h in highlights] == [1, 2]
    assert (
        text_under(pdf, highlights) == "с даты приёмки. 1.3. Гарантийный срок составляет 12 месяцев"
    )


def test_empty_and_whitespace_spans(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    newline = doc.text.index("\n")
    with Highlighter(pdf, doc) as highlighter:
        assert highlighter.locate(newline, newline + 1) == []
        assert highlighter.locate(5, 5) == []


def test_as_dict(setup: tuple[Path, ExtractedDocument]) -> None:
    pdf, doc = setup
    data = locate(pdf, doc, "Гарантийный срок")[0].as_dict()
    assert data["page"] == 2 and len(data["rects"]) == 1 and len(data["rects"][0]) == 4


def test_quote_across_table_cells(tmp_path: Path) -> None:
    markup = "**Наименование** | **Ед. изм.** | **Количество**\nБумага SvetoCopy А4 | пачка | 500"
    pdf = build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / "t.pdf")
    doc = extract_document(pdf)
    highlights = locate(pdf, doc, "Бумага SvetoCopy А4 | пачка | 500")
    assert len(highlights[0].rects) == 1  # целый ряд — одним прямоугольником
    partial = locate(pdf, doc, "SvetoCopy А4 | пачка")
    assert len(partial[0].rects) == 2  # по прямоугольнику на ячейку
    assert text_under(pdf, partial) == "SvetoCopy А4 пачка"
