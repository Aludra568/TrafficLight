"""Генерация тестовых PDF с русским текстом.

Шрифты Noto из pymupdf-fonts — результат одинаков на Windows и Linux.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

PAGE_W, PAGE_H = 595, 842  # A4 в пунктах
MARGIN_X, TOP, BOTTOM = 56, 72, 790
REGULAR, BOLD = "notos", "notosbo"


@dataclass
class PdfLine:
    text: str
    size: float = 11
    bold: bool = False
    x: float = MARGIN_X
    same_row: bool = False  # на той же высоте, что и предыдущая строка (ячейка таблицы)


@dataclass
class PdfSpec:
    pages: list[list[PdfLine]]
    header: str | None = None  # повторяется на каждой странице
    footer_page_numbers: bool = False  # «Страница N из M»
    scanned_pages: set[int] = field(default_factory=set)  # 1-based: страница-картинка без текста


def L(text: str, size: float = 11, bold: bool = False) -> PdfLine:  # noqa: N802 — короткий конструктор
    return PdfLine(text, size, bold)


def table_row(cells: list[str], bold: bool = False) -> list[PdfLine]:
    step = (PAGE_W - 2 * MARGIN_X) / len(cells)
    return [
        PdfLine(cell, bold=bold, x=MARGIN_X + i * step, same_row=i > 0)
        for i, cell in enumerate(cells)
    ]


def pages_from_markup(markup: str) -> list[list[PdfLine]]:
    """Короткая запись документа для тестов.

    `===` — новая страница; `!! текст` — заголовок 14 pt жирным; `**текст**` — жирный;
    `a | b | c` — строка таблицы (ячейки на одной высоте); пустые строки пропускаются.
    """
    pages: list[list[PdfLine]] = [[]]
    for raw in markup.strip().splitlines():
        text = raw.strip()
        if not text:
            continue
        if text == "===":
            pages.append([])
        elif text.startswith("!! "):
            pages[-1].append(PdfLine(text[3:], size=14, bold=True))
        elif " | " in text:
            cells = [c.strip() for c in text.split(" | ")]
            bold = all(c.startswith("**") for c in cells)
            pages[-1].extend(table_row([c.strip("*") for c in cells], bold=bold))
        elif text.startswith("**") and text.endswith("**"):
            pages[-1].append(PdfLine(text[2:-2], bold=True))
        else:
            pages[-1].append(PdfLine(text))
    return pages


def build_pdf(spec: PdfSpec, path: Path) -> Path:
    doc = pymupdf.open()
    total = len(spec.pages)
    for number, lines in enumerate(spec.pages, start=1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_font(fontname=REGULAR, fontbuffer=pymupdf.Font(REGULAR).buffer)
        page.insert_font(fontname=BOLD, fontbuffer=pymupdf.Font(BOLD).buffer)
        if number in spec.scanned_pages:
            # «Скан»: картинка без текстового слоя
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 280), False)
            pix.clear_with(200)
            page.insert_image(pymupdf.Rect(MARGIN_X, TOP, PAGE_W - MARGIN_X, BOTTOM), pixmap=pix)
            continue
        if spec.header:
            page.insert_text((MARGIN_X, 40), spec.header, fontname=REGULAR, fontsize=8)
        if spec.footer_page_numbers:
            page.insert_text(
                (PAGE_W / 2 - 40, 815),
                f"Страница {number} из {total}",
                fontname=REGULAR,
                fontsize=8,
            )
        y = TOP
        for line in lines:
            if not line.same_row:
                y += line.size * 1.6
            if y > BOTTOM:
                raise ValueError(f"страница {number} переполнена — разбейте текст на страницы")
            page.insert_text(
                (line.x, y),
                line.text,
                fontname=BOLD if line.bold else REGULAR,
                fontsize=line.size,
            )
    doc.save(path)
    doc.close()
    return path
