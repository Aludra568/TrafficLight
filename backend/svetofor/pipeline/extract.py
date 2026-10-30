"""Текст PDF по страницам и строкам со шрифтами.

Результат — единый текст документа со смещениями страниц и строк. Колонтитулы и номера
страниц, повторяющиеся на большинстве страниц, исключаются: они мешали бы поиску, цитатам
и разбору структуры.
"""

from __future__ import annotations

import bisect
import re
import statistics
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pymupdf

from svetofor.pipeline.pdf import open_pdf
from svetofor.pipeline.textlayer import is_garbage, is_mojibake, repair_mojibake

_EDGE = 0.08  # верхние и нижние 8% страницы — зона колонтитулов
_PAGE_NUMBER = re.compile(
    r"^(?:стр(?:аница)?\.?\s*)?[-–—\s]*\d{1,4}(?:\s*(?:из|/)\s*\d{1,4})?[-–—\s]*$", re.I
)
_BOLD_FLAG = 16
# номер пункта без текста: «1.», «1.1», «2.3.1.», «1)», «а)»
_BARE_NUMBER = re.compile(r"^(?:\d{1,2}(?:\.\d{1,3}){0,4}\.?|\d{1,2}\)|[а-яa-z]\))$")
CELL_SEPARATOR = " | "
_LIST_GAP = 72.0  # наибольший разрыв между номером и текстом, пт (1 дюйм)


@dataclass(frozen=True)
class Line:
    page: int  # с 1
    text: str
    size: float
    bold: bool
    bbox: tuple[float, float, float, float]
    char_start: int  # смещения в тексте документа
    char_end: int
    cells: int = 1  # >1 — ряд таблицы, склеенный из ячеек через CELL_SEPARATOR
    hidden: bool = False  # не видна при просмотре: белым по белому, крошечный шрифт, за полями


@dataclass(frozen=True)
class PageInfo:
    number: int
    text: str
    char_start: int
    width: float
    height: float

    @property
    def char_end(self) -> int:
        return self.char_start + len(self.text)


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    pages: tuple[PageInfo, ...]
    lines: tuple[Line, ...]
    body_size: float  # типичный размер шрифта основного текста

    @property
    def hidden_lines(self) -> tuple[Line, ...]:
        return tuple(line for line in self.lines if line.hidden)

    def page_at(self, offset: int) -> int:
        """Номер страницы (с 1) для смещения в тексте документа."""
        starts = [p.char_start for p in self.pages]
        index = max(0, bisect.bisect_right(starts, offset) - 1)
        return self.pages[index].number if self.pages else 1


@dataclass
class _RawLine:
    page: int
    text: str
    size: float
    bold: bool
    bbox: tuple[float, float, float, float]
    in_edge: bool
    cells: int = 1
    hidden: bool = False


# управляющие символы из битых PDF: NUL PostgreSQL не примет в text, остальные — мусор
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _boilerplate_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


_WHITE_MIN = 0xF0  # каждый канал цвета не темнее — текст «белый»
_TINY_FONT = 1.5  # пт: такой текст глазом не прочитать
_OFF_PAGE = 2.0  # пт за краем листа — уже не видно
# Без обрезки по листу: PyMuPDF по умолчанию не отдаёт текст за его краем — и условие, спрятанное
# за полем, не увидели бы ни правила, ни детектор скрытого текста
_TEXT_FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_MEDIABOX_CLIP
_EVERYWHERE = cast(Any, pymupdf).INFINITE_RECT()  # без флага PyMuPDF всё равно режет по листу


def _is_white(color: int) -> bool:
    return all((color >> shift) & 0xFF >= _WHITE_MIN for shift in (16, 8, 0))


class _Backgrounds:
    """Цветные заливки и картинки страницы — белый текст на них виден (шапка таблицы и т. п.)."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._rects: list[tuple[float, float, float, float]] | None = None

    def under(self, bbox: tuple[float, float, float, float]) -> bool:
        if self._rects is None:
            self._rects = self._collect()
        x = (bbox[0] + bbox[2]) / 2
        y = (bbox[1] + bbox[3]) / 2
        return any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in self._rects)

    def _collect(self) -> list[tuple[float, float, float, float]]:
        rects: list[tuple[float, float, float, float]] = []
        try:
            for drawing in self._page.get_drawings():
                fill = drawing.get("fill")
                if fill is not None and not all(channel >= _WHITE_MIN / 255 for channel in fill):
                    rect = drawing["rect"]
                    rects.append((rect.x0, rect.y0, rect.x1, rect.y1))
            for image in self._page.get_image_info():
                rects.append(tuple(image["bbox"]))
        except Exception:  # без фона — считаем текст видимым: лучше пропустить, чем оклеветать
            return [(-1e9, -1e9, 1e9, 1e9)]
        return rects


def _is_hidden(
    spans: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    page_rect: Any,
    backgrounds: _Backgrounds,
) -> bool:
    """Строка есть в тексте файла, но человек её не увидит.

    Белый по белому без заливки и картинки под ним, шрифт меньше 1,5 пт, целиком за полями
    страницы. Невидимый слой распознанного скана сюда не попадает: он не белый и не крошечный.
    """
    if max(s["size"] for s in spans) < _TINY_FONT:
        return True
    x0, y0, x1, y1 = bbox
    # за полями целиком или частью: «хвост» строки за краем листа на экране и при печати
    # обрезан — «срок оплаты может быть продлён…» человек дочитает лишь до края
    if (
        x0 < page_rect.x0 - _OFF_PAGE
        or x1 > page_rect.x1 + _OFF_PAGE
        or y0 < page_rect.y0 - _OFF_PAGE
        or y1 > page_rect.y1 + _OFF_PAGE
    ):
        return True
    return all(_is_white(int(s.get("color", 0))) for s in spans) and not backgrounds.under(bbox)


def _page_lines(page: Any, number: int) -> list[_RawLine]:
    # координаты текста — в системе неповёрнутого листа, а `page.rect` — с учётом поворота:
    # на альбомной странице (поворот 90°) низ таблицы иначе оказывался «за краем листа»
    bounds = (page.rect * page.derotation_matrix).normalize()
    height = bounds.height or 1.0
    lines: list[_RawLine] = []
    backgrounds = _Backgrounds(page)
    data = page.get_text("dict", sort=True, flags=_TEXT_FLAGS, clip=_EVERYWHERE)
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            text = _CONTROL.sub(" ", "".join(s["text"] for s in line.get("spans", []))).strip()
            if not spans or not text:
                continue
            size = max(s["size"] for s in spans)
            bold = all((s["flags"] & _BOLD_FLAG) or "bold" in s["font"].lower() for s in spans)
            x0, y0, x1, y1 = line["bbox"]
            in_edge = y1 < height * _EDGE or y0 > height * (1 - _EDGE)
            hidden = _is_hidden(spans, (x0, y0, x1, y1), bounds, backgrounds)
            lines.append(
                _RawLine(number, text, round(size, 1), bold, (x0, y0, x1, y1), in_edge, 1, hidden)
            )
    return lines


def _same_row(a: _RawLine, b: _RawLine) -> bool:
    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    return overlap > 0.5 * min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])


def _join_list_numbers(lines: list[_RawLine]) -> list[_RawLine]:
    """Склеить номер пункта с текстом, если между ними табуляция («1.1.→Поставщик…»).

    Word отделяет автонумерацию табуляцией, и PyMuPDF отдаёт номер отдельной строкой в том же
    ряду. Без склейки пункт потерял бы номер, а текст — привязку к разделу.
    """
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for i, line in enumerate(lines):
        if not _BARE_NUMBER.match(line.text):
            continue
        best: tuple[float, int] | None = None
        for j, other in enumerate(lines):
            gap = other.bbox[0] - line.bbox[2]
            if j == i or j in taken or j in pairs or not -1 <= gap <= _LIST_GAP:
                continue
            if _same_row(line, other) and (best is None or gap < best[0]):
                best = (gap, j)
        if best is not None:
            pairs[i] = best[1]
            taken.add(best[1])
    result: list[_RawLine] = []
    for i, line in enumerate(lines):
        if i in taken:
            continue
        if i in pairs:
            other = lines[pairs[i]]
            bbox = (
                min(line.bbox[0], other.bbox[0]),
                min(line.bbox[1], other.bbox[1]),
                max(line.bbox[2], other.bbox[2]),
                max(line.bbox[3], other.bbox[3]),
            )
            line = _RawLine(
                line.page,
                f"{line.text} {other.text}",
                max(line.size, other.size),
                other.bold,
                bbox,
                line.in_edge and other.in_edge,
                hidden=line.hidden and other.hidden,
            )
        result.append(line)
    return result


def _join_table_rows(lines: list[_RawLine]) -> list[_RawLine]:
    """Ячейки одного ряда — в одну строку «Бумага А4 | пачка | 100», слева направо.

    Иначе ряд таблицы распадается на отдельные строки, и связь «товар — количество» теряется
    и для поиска, и для модели. Порядок рядов — сверху вниз.
    """
    rows: list[list[_RawLine]] = []
    for line in sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])):
        row = next((r for r in rows if _same_row(r[0], line)), None)
        if row is None:
            rows.append([line])
        else:
            row.append(line)
    result: list[_RawLine] = []
    for row in rows:
        if len(row) == 1:
            result.append(row[0])
            continue
        row.sort(key=lambda item: item.bbox[0])
        result.append(
            _RawLine(
                page=row[0].page,
                text=CELL_SEPARATOR.join(cell.text for cell in row),
                size=max(cell.size for cell in row),
                bold=all(cell.bold for cell in row),
                bbox=(
                    min(c.bbox[0] for c in row),
                    min(c.bbox[1] for c in row),
                    max(c.bbox[2] for c in row),
                    max(c.bbox[3] for c in row),
                ),
                in_edge=all(c.in_edge for c in row),
                cells=sum(c.cells for c in row),
                hidden=all(c.hidden for c in row),
            )
        )
    return result


def _raw_lines(doc: Any, max_pages: int) -> tuple[list[_RawLine], list[tuple[float, float]]]:
    lines: list[_RawLine] = []
    sizes: list[tuple[float, float]] = []
    for index, page in enumerate(doc):
        if index >= max_pages:
            break
        sizes.append((page.rect.width, page.rect.height))
        page_lines = _page_lines(page, index + 1)
        if is_mojibake(" ".join(line.text for line in page_lines)):
            page_lines = [replace(line, text=repair_mojibake(line.text)) for line in page_lines]
        lines.extend(_join_table_rows(_join_list_numbers(page_lines)))
    return lines, sizes


def _boilerplate(lines: list[_RawLine], page_count: int) -> set[tuple[int, str]]:
    """Повторы колонтитулов: в зоне полей и повторяются (с точностью до чисел) на ≥60% страниц.

    Первое появление колонтитула остаётся в тексте: в колонтитуле может стоять условие
    («Пеня 1/100 цены контракта…»), и удалённое целиком оно пропадало бы и для правил, и для
    модели. Номера страниц удаляются все.
    """
    edge = [line for line in lines if line.in_edge]
    pages_by_key: dict[str, set[int]] = {}
    for line in edge:
        pages_by_key.setdefault(_boilerplate_key(line.text), set()).add(line.page)
    repeating = {
        k for k, pages in pages_by_key.items() if page_count >= 3 and len(pages) >= 0.6 * page_count
    }
    drop: set[tuple[int, str]] = set()
    kept_once: set[str] = set()
    for line in edge:
        key = _boilerplate_key(line.text)
        if _PAGE_NUMBER.match(line.text):
            drop.add((line.page, line.text))
        elif key in repeating:
            if key in kept_once:
                drop.add((line.page, line.text))
            kept_once.add(key)
    return drop


def extract_document(pdf_path: Path, max_pages: int = 1000) -> ExtractedDocument:
    with open_pdf(pdf_path) as doc:
        raw, page_sizes = _raw_lines(doc, max_pages)
    drop = _boilerplate(raw, len(page_sizes))
    kept = [line for line in raw if (line.page, line.text) not in drop]

    body_candidates = Counter(line.size for line in kept)
    body_size = body_candidates.most_common(1)[0][0] if body_candidates else 11.0
    if len(body_candidates) > 1:
        weighted = [line.size for line in kept for _ in range(max(1, len(line.text) // 40))]
        body_size = statistics.median_low(weighted)

    pages: list[PageInfo] = []
    lines: list[Line] = []
    parts: list[str] = []
    offset = 0
    for number, (width, height) in enumerate(page_sizes, start=1):
        page_lines = [line for line in kept if line.page == number]
        page_start = offset
        page_parts: list[str] = []
        for i, line in enumerate(page_lines):
            if i:
                page_parts.append("\n")
                offset += 1
            lines.append(
                Line(
                    number,
                    line.text,
                    line.size,
                    line.bold,
                    line.bbox,
                    offset,
                    offset + len(line.text),
                    line.cells,
                    line.hidden,
                )
            )
            page_parts.append(line.text)
            offset += len(line.text)
        page_text = "".join(page_parts)
        pages.append(PageInfo(number, page_text, page_start, width, height))
        parts.append(page_text)
        if number < len(page_sizes):
            parts.append("\n")
            offset += 1
    return ExtractedDocument("".join(parts), tuple(pages), tuple(lines), float(body_size))


def scanned_pages(pdf_path: Path, min_chars: int = 50) -> list[int]:
    """Страницы, которым нужен OCR: почти нет текстового слоя, но есть изображения."""
    return pages_needing_ocr(pdf_path, min_chars)[0]


def pages_needing_ocr(pdf_path: Path, min_chars: int = 50) -> tuple[list[int], list[int]]:
    """(сканы без текста, страницы с нечитаемым текстовым слоем) — обоим нужен OCR.

    «Кракозябры» cp1251 не в счёт: их чинит извлечение текста без распознавания.
    """
    scans: list[int] = []
    garbage: list[int] = []
    with open_pdf(pdf_path) as doc:
        for index, page in enumerate(doc):
            text = page.get_text("text").strip()
            if len(text) < min_chars:
                if page.get_images(full=False):
                    scans.append(index + 1)
            elif not is_mojibake(text) and is_garbage(text):
                garbage.append(index + 1)
    return scans, garbage
