"""Фрагменты для поиска и для промптов.

Фрагмент — пункт или несколько соседних пунктов одного раздела; крупный пункт режется по абзацам
с перекрытием в один абзац. Текст фрагмента — точный отрезок текста документа (смещения нужны
для проверки цитат и подсветки), поэтому ничего не переписывается и не теряется: каждый
непробельный символ документа попадает хотя бы в один фрагмент.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from svetofor.pipeline.extract import ExtractedDocument
from svetofor.pipeline.structure import DocumentStructure, SectionNode
from svetofor.rules.verify import normalize

CHARS_PER_TOKEN = 3.5  # русский текст в токенизаторах bge-m3 и чат-моделей — с запасом
MIN_TOKENS = 120  # фрагмент меньше этого присоединяет следующий пункт того же раздела

_PARAGRAPH_BREAK = re.compile(r"(?<=[.;:!?])\n(?=[ \t]*[А-ЯЁA-Z0-9«\"(•\-–—])")
_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+(?=[А-ЯЁA-Z0-9«\"(])")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) if text.strip() else 0


@dataclass(frozen=True)
class ChunkDraft:
    order: int
    section_order: int | None  # узел структуры, к которому относится фрагмент
    section_path: str
    text: str
    text_norm: str
    char_start: int
    char_end: int
    page_from: int
    page_to: int
    token_count: int

    @property
    def embedding_text(self) -> str:
        """Путь раздела в начале: поиск находит пункт, даже если тема названа только в заголовке."""
        return f"{self.section_path}: {self.text}"


@dataclass(frozen=True)
class _Unit:
    node: SectionNode
    start: int
    end: int


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _span_tokens(start: int, end: int) -> int:
    return math.ceil((end - start) / CHARS_PER_TOKEN)


def _split_by(text: str, start: int, end: int, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in pattern.finditer(text, start, end):
        piece = _trim(text, cursor, match.start())
        if piece[0] < piece[1]:
            spans.append(piece)
        cursor = match.end()
    last = _trim(text, cursor, end)
    if last[0] < last[1]:
        spans.append(last)
    return spans


def _hard_split(text: str, start: int, end: int, max_tokens: int) -> list[tuple[int, int]]:
    """Последнее средство: резать по пробелам на куски не длиннее max_tokens."""
    limit = int(max_tokens * CHARS_PER_TOKEN)
    spans: list[tuple[int, int]] = []
    while end - start > limit:
        cut = text.rfind(" ", start, start + limit)
        cut = cut if cut > start else start + limit
        spans.append(_trim(text, start, cut))
        start = _trim(text, cut, end)[0]
    if start < end:
        spans.append((start, end))
    return spans


def _pieces(text: str, start: int, end: int, max_tokens: int) -> list[tuple[int, int]]:
    """Абзацы; слишком длинный абзац — предложения; слишком длинное предложение — по пробелам."""
    result: list[tuple[int, int]] = []
    for p_start, p_end in _split_by(text, start, end, _PARAGRAPH_BREAK):
        if _span_tokens(p_start, p_end) <= max_tokens:
            result.append((p_start, p_end))
            continue
        for s_start, s_end in _split_by(text, p_start, p_end, _SENTENCE_BREAK):
            if _span_tokens(s_start, s_end) <= max_tokens:
                result.append((s_start, s_end))
            else:
                result.extend(_hard_split(text, s_start, s_end, max_tokens))
    return result


def _windows(pieces: list[tuple[int, int]], max_tokens: int) -> list[tuple[int, int]]:
    """Окна из подряд идущих кусков; следующее окно начинается с последнего куска предыдущего."""
    windows: list[tuple[int, int]] = []
    i = 0
    while i < len(pieces):
        j = i + 1
        while j < len(pieces) and _span_tokens(pieces[i][0], pieces[j][1]) <= max_tokens:
            j += 1
        windows.append((pieces[i][0], pieces[j - 1][1]))
        if j >= len(pieces):
            break
        overlap = j - 1 > i and _span_tokens(*pieces[j - 1]) <= max_tokens // 2
        i = j - 1 if overlap else j
    return windows


def _common_ancestor(structure: DocumentStructure, units: list[_Unit]) -> SectionNode:
    def chain(node: SectionNode) -> list[int]:
        orders = [node.order]
        while node.parent is not None:
            node = structure.nodes[node.parent]
            orders.append(node.order)
        return orders

    common = chain(units[0].node)
    for unit in units[1:]:
        ancestors = set(chain(unit.node))
        common = [order for order in common if order in ancestors]
    return structure.nodes[common[0]] if common else units[0].node


def _can_join(group: list[_Unit], unit: _Unit, has_children: set[int]) -> bool:
    anchor = group[0].node
    if unit.node.order in has_children:
        return False  # заголовок с пунктами начинает свой фрагмент — вместе со своими пунктами
    if anchor.order in has_children:
        return unit.node.parent == anchor.order  # к заголовку — только его собственные пункты
    return unit.node.parent == anchor.parent  # к пункту — соседние пункты того же раздела


def _group_units(
    units: list[_Unit], structure: DocumentStructure, max_tokens: int, min_tokens: int
) -> list[list[_Unit]]:
    has_children = {n.parent for n in structure.nodes if n.parent is not None}
    groups: list[list[_Unit]] = []
    for unit in units:
        if groups:
            group = groups[-1]
            small = _span_tokens(group[0].start, group[-1].end) < min_tokens
            fits = _span_tokens(group[0].start, unit.end) <= max_tokens
            if small and fits and _can_join(group, unit, has_children):
                group.append(unit)
                continue
        groups.append([unit])
    return groups


def make_chunks(
    doc: ExtractedDocument,
    structure: DocumentStructure,
    max_tokens: int = 700,
    min_tokens: int = MIN_TOKENS,
) -> list[ChunkDraft]:
    units = []
    for node in structure.nodes:
        start, end = _trim(doc.text, node.char_start, node.own_end)
        if start < end:
            units.append(_Unit(node, start, end))

    chunks: list[ChunkDraft] = []
    for group in _group_units(units, structure, max_tokens, min_tokens):
        section = _common_ancestor(structure, group)
        start, end = group[0].start, group[-1].end
        spans = (
            _windows(_pieces(doc.text, start, end, max_tokens), max_tokens)
            if _span_tokens(start, end) > max_tokens
            else [(start, end)]
        )
        for span_start, span_end in spans:
            text = doc.text[span_start:span_end]
            chunks.append(
                ChunkDraft(
                    order=len(chunks),
                    section_order=section.order,
                    section_path=section.path,
                    text=text,
                    text_norm=normalize(text),
                    char_start=span_start,
                    char_end=span_end,
                    page_from=doc.page_at(span_start),
                    page_to=doc.page_at(span_end - 1),
                    token_count=estimate_tokens(text),
                )
            )
    return chunks
