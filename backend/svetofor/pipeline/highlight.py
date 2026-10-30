"""Координаты подсветки подтверждённых цитат.

Цитата после проверки (`rules/verify.py`) — это точный отрезок текста документа. По смещениям
находятся строки, которые он задевает: строка целиком — её прямоугольник; часть строки —
поиск этой части только внутри прямоугольника строки. Поиск по всей странице не годится:
типовые обороты («в течение 10 рабочих дней») повторяются, и подсветилось бы не то место.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from svetofor.pipeline.extract import CELL_SEPARATOR, ExtractedDocument, Line
from svetofor.pipeline.pdf import open_pdf

Rect = tuple[float, float, float, float]
_CLIP_PAD = 1.5  # пт: запас вокруг строки, чтобы поиск не терял крайние символы


@dataclass(frozen=True)
class Highlight:
    page: int  # с 1
    rects: tuple[Rect, ...]  # в координатах PDF (пункты, начало — левый верхний угол)

    def as_dict(self) -> dict[str, Any]:
        return {"page": self.page, "rects": [list(r) for r in self.rects]}


def _round(rect: Any) -> Rect:
    return (round(rect[0], 2), round(rect[1], 2), round(rect[2], 2), round(rect[3], 2))


def _proportional(line: Line, a: int, b: int) -> Rect:
    """Запасной вариант, если поиск внутри строки не сработал: доля ширины по числу символов."""
    x0, y0, x1, y1 = line.bbox
    width = (x1 - x0) / max(len(line.text), 1)
    return _round((x0 + a * width, y0, x0 + b * width, y1))


class Highlighter:
    """Открывает PDF один раз на документ: `with Highlighter(pdf, doc) as h: h.locate(s, e)`."""

    def __init__(self, pdf_path: Path, doc: ExtractedDocument) -> None:
        self.doc = doc
        self._pdf = open_pdf(pdf_path)
        self._starts = [line.char_start for line in doc.lines]

    def __enter__(self) -> Highlighter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._pdf.close()

    def _lines_between(self, start: int, end: int) -> list[Line]:
        first = max(0, bisect.bisect_right(self._starts, start) - 1)
        result = []
        for line in self.doc.lines[first:]:
            if line.char_start >= end:
                break
            if line.char_end > start:
                result.append(line)
        return result

    def _part_rects(self, line: Line, a: int, b: int) -> list[Rect]:
        """Часть строки; у ряда таблицы — по прямоугольнику на каждую задетую ячейку.

        Разделителя ячеек « | » в самом PDF нет, поэтому ячейки ищутся по отдельности.
        """
        if line.cells == 1 or CELL_SEPARATOR not in line.text[a:b]:
            return [self._part_rect(line, a, b)]
        rects = []
        position = a
        for piece in line.text[a:b].split(CELL_SEPARATOR):
            start = line.text.index(piece, position) if piece else position
            if piece.strip():
                rects.append(self._part_rect(line, start, start + len(piece)))
            position = start + len(piece) + len(CELL_SEPARATOR)
        return rects

    def _part_rect(self, line: Line, a: int, b: int) -> Rect:
        """Часть строки. Если фраза встречается в строке дважды — берём ближайшую к расчётной."""
        estimate = _proportional(line, a, b)
        page = self._pdf[line.page - 1]
        x0, y0, x1, y1 = line.bbox
        clip = (x0 - _CLIP_PAD, y0 - _CLIP_PAD, x1 + _CLIP_PAD, y1 + _CLIP_PAD)
        hits = page.search_for(line.text[a:b], clip=clip)
        if not hits:
            return estimate
        hit = min(hits, key=lambda r: abs(r.x0 - estimate[0]))
        return _round((hit.x0, hit.y0, hit.x1, hit.y1))

    def locate(self, start: int, end: int) -> list[Highlight]:
        """Прямоугольники для отрезка [start, end) текста документа, по страницам."""
        by_page: dict[int, list[Rect]] = {}
        for line in self._lines_between(start, end):
            a = max(start, line.char_start) - line.char_start
            b = min(end, line.char_end) - line.char_start
            while a < b and line.text[a].isspace():
                a += 1
            while b > a and line.text[b - 1].isspace():
                b -= 1
            if a >= b:
                continue
            whole = a == 0 and b == len(line.text)  # целый ряд таблицы — одним прямоугольником
            rects = [_round(line.bbox)] if whole else self._part_rects(line, a, b)
            by_page.setdefault(line.page, []).extend(rects)
        return [Highlight(page, tuple(rects)) for page, rects in sorted(by_page.items())]
