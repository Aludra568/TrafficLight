"""Разделы и пункты документа.

Дерево нужно для точной «ссылки на раздел» в замечаниях и для нарезки фрагментов.
Признаки заголовка по убыванию надёжности: стили DOCX (подсказки), нумерация в начале строки,
«Раздел/Глава/Статья N», «Приложение № N», оформление (жирный, крупнее основного, ПРОПИСНЫЕ).

Главная опасность — ложные заголовки: перенос строки «…в течение\\n10 (десяти) дней»,
ссылка «…согласно пункту\\n3.1 Контракта», дата, процент, шапка таблицы. Поэтому:
- номер без точки («10 дней») — заголовок только при выделении шрифтом;
- после номера должна идти заглавная буква или кавычка;
- номер должен продолжать текущую нумерацию (первый дочерний или следующий соседний пункт);
- заголовок по оформлению должен стоять в строке один (не ячейка таблицы).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from svetofor.pipeline.extract import ExtractedDocument, Line

PATH_SEPARATOR = " › "
_LABEL_MAX = 80
_TITLE_MAX = 200
_MAX_TITLE_LINES = 3

_NUMBERED = re.compile(r"^(?P<num>[1-9]\d?(?:\.\d{1,3}){0,4})(?P<sep>\.\s*|\s+)(?P<rest>\S.*)$")
_KEYWORD = re.compile(
    r"^(?P<kw>раздел|часть|глава|статья)\s+(?P<num>[1-9]\d{0,2}|[IVXLC]{1,7})(?![\w])\.?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_APPENDIX = re.compile(
    r"^приложение(?![\w])(?:\s*№?\s*(?P<num>\d{1,3}(?:\.\d{1,2})?))?", re.IGNORECASE
)
_ORG_FORM = re.compile(
    r"^(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП|ГУП|МУП|ФГУП|ГБУ|ГКУ|ГАУ|МБУ|МКУ|МАУ|ФГБУ|ФГКУ|ФКУ|ФГАУ"
    r"|ФГБОУ|ГБОУ|МБОУ)(?![\w])"
)
# слова, на которых обрывается перенесённая строка заголовка: «Обязанности Поставщика и / Заказчика»
_CONNECTORS = frozenset(
    {
        "и",
        "или",
        "а",
        "также",
        "в",
        "во",
        "на",
        "к",
        "ко",
        "по",
        "с",
        "со",
        "о",
        "об",
        "для",
        "при",
        "от",
        "до",
        "за",
        "из",
        "без",
        "между",
        "под",
        "над",
    }
)
_KEYWORD_RANK = {"раздел": 2.0, "часть": 2.0, "глава": 3.0, "статья": 4.0}
_APPENDIX_RANK = 1.0
_SECTION_RANK = 4.5  # крупный заголовок без номера — на уровне разделов
_LEAF_RANK = 100.0  # вводная часть: детей не бывает


class SectionKind(StrEnum):
    PREAMBLE = "preamble"  # текст до первого заголовка
    APPENDIX = "appendix"  # «Приложение № 1»
    PART = "part"  # «Раздел 2», «Глава 3», «Статья 5»
    HEADING = "heading"  # заголовок раздела с номером или без
    CLAUSE = "clause"  # пункт без заголовка: «7.3. Заказчик вправе…»


@dataclass(frozen=True)
class SectionNode:
    order: int
    parent: int | None
    kind: SectionKind
    number: str | None  # «7.3»
    title: str
    label: str  # как узел выглядит в пути
    level: int  # глубина, с 1
    path: str  # «Проект контракта › 7. Ответственность сторон › 7.3»
    char_start: int
    own_end: int  # конец собственного текста (до первого вложенного узла)
    char_end: int  # конец вместе с вложенными
    page_from: int
    page_to: int


@dataclass(frozen=True)
class DocumentStructure:
    title: str | None  # заголовок документа с первой страницы («ПРОЕКТ КОНТРАКТА»)
    nodes: tuple[SectionNode, ...]

    def node_at(self, offset: int) -> SectionNode | None:
        """Самый глубокий узел, в собственный текст которого попадает смещение."""
        starts = [n.char_start for n in self.nodes]
        index = bisect.bisect_right(starts, offset) - 1
        return self.nodes[index] if index >= 0 else None


@dataclass
class _Candidate:
    kind: SectionKind
    rank: float
    number: str | None
    parts: tuple[int, ...] | None
    title: str
    label: str
    styled: bool  # выделен оформлением или стилем DOCX — нумерацию не сверяем
    typographic: bool = False  # заголовок только по оформлению, без номера
    hinted: bool = False  # из стилей DOCX


@dataclass
class _Open:
    rank: float
    order: int
    parts: tuple[int, ...] | None


def _letters(text: str) -> list[str]:
    return [c for c in text if c.isalpha()]


def _is_upper(text: str) -> bool:
    letters = _letters(text)
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def _ends_sentence(text: str, allowed: str = "") -> bool:
    return bool(text) and text[-1] in ".,;:" and text[-1] not in allowed


def _looks_numbered(text: str) -> bool:
    """Строка начинается как пункт: «2.1. Цена…», а не «15.01.2026 года» или «10 дней»."""
    match = _NUMBERED.match(text)
    if not match:
        return False
    first = match["rest"][:1]
    return ("." in match["num"] or match["sep"].startswith(".")) and (
        first.isupper() or first in "«\"'("
    )


def _heading_shaped(text: str) -> bool:
    """Короткая строка без конца предложения: «Статья 5. Цена контракта», «Раздел II»."""
    return len(text) <= 100 and not _ends_sentence(text) and ". " not in text.split(". ", 1)[-1]


def _short(text: str, limit: int = _LABEL_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _norm_key(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _alone_on_row(lines: Sequence[Line]) -> list[bool]:
    """Строка одна на своей высоте — иначе это ячейка таблицы или колонка реквизитов."""
    alone = [line.cells == 1 for line in lines]  # склеенный ряд таблицы — не заголовок
    by_page: dict[int, list[int]] = {}
    for i, line in enumerate(lines):
        by_page.setdefault(line.page, []).append(i)
    for indexes in by_page.values():
        indexes.sort(key=lambda i: lines[i].bbox[1])
        for pos, i in enumerate(indexes):
            _, y0, _, y1 = lines[i].bbox
            for j in indexes[pos + 1 :]:
                oy0, oy1 = lines[j].bbox[1], lines[j].bbox[3]
                if oy0 >= y1:
                    break
                overlap = min(y1, oy1) - max(y0, oy0)
                if overlap > 0.5 * min(y1 - y0, oy1 - oy0):
                    alone[i] = alone[j] = False
    return alone


class _Classifier:
    def __init__(
        self, doc: ExtractedDocument, hints: Mapping[str, int], alone: Sequence[bool]
    ) -> None:
        self.doc = doc
        self.lines = doc.lines
        self.hints = hints
        self.alone = alone
        self.first_on_page = {
            i for i, line in enumerate(self.lines) if i == 0 or self.lines[i - 1].page != line.page
        }

    def styled(self, i: int, text: str | None = None) -> bool:
        line = self.lines[i]
        return line.bold or line.size >= self.doc.body_size + 1 or _is_upper(text or line.text)

    def continues(self, i: int) -> bool:
        """Следующая строка продолжает предложение (начинается со строчной буквы)."""
        if i + 1 >= len(self.lines):
            return False
        first = self.lines[i + 1].text[:1]
        return first.isalpha() and first.islower()

    def opens_section(self, i: int) -> bool:
        """Сразу за строкой идёт пронумерованный пункт или выделенный заголовок.

        Только так можно отличить невыделенный заголовок «1. Предмет контракта» от первой строки
        длинного пункта: продолжение пункта часто начинается с заглавной («Поставщика», «Товар» —
        определённые в контракте термины), поэтому регистр следующей строки ничего не доказывает.
        """
        if i + 1 >= len(self.lines):
            return True
        following = self.lines[i + 1].text
        keyword_heading = bool(_KEYWORD.match(following)) and _heading_shaped(following)
        return _looks_numbered(following) or keyword_heading or self.styled(i + 1)

    def _hint_keys(self, text: str) -> list[str]:
        """Текст строки и он же без номера: автонумерация Word не входит в текст абзаца DOCX."""
        keys = [_norm_key(text)]
        match = _NUMBERED.match(text)
        if match:
            keys.append(_norm_key(match["rest"]))
        return keys

    def hint_level(self, i: int) -> int | None:
        """Уровень заголовка из стилей DOCX. Длинный заголовок в PDF переносится — тогда
        первая строка совпадает с началом подсказки."""
        for key in self._hint_keys(self.lines[i].text):
            if key in self.hints:
                return self.hints[key]
            if len(key) >= 12:
                for hint, level in self.hints.items():
                    if hint.startswith(key + " "):
                        return level
        return None

    def hint_continues(self, first: int, j: int) -> bool:
        """Строки first..j вместе всё ещё совпадают с началом подсказки из стилей DOCX."""
        texts = " ".join(line.text for line in self.lines[first : j + 1])
        return any(
            key in self.hints or any(h.startswith(key + " ") for h in self.hints)
            for key in self._hint_keys(texts)
        )

    def classify(self, i: int) -> _Candidate | None:
        line = self.lines[i]
        if not self.alone[i] or not line.text[:1].strip():
            return None
        return self._numbered(i) or self._keyword(i) or self._appendix(i) or self._typographic(i)

    def _numbered(self, i: int) -> _Candidate | None:
        match = _NUMBERED.match(self.lines[i].text)
        if not match:
            return None
        rest, number = match["rest"].strip(), match["num"]
        hinted = self.hint_level(i) is not None
        styled = hinted or self.styled(i, rest)
        has_dot = "." in number or match["sep"].startswith(".")
        if not has_dot and not styled:
            return None  # «10 (десяти) дней» — перенос строки, а не пункт
        first = rest[:1]
        if not (first.isupper() or first in "«\"'(" or (styled and first.isalpha())):
            return None  # «1.5 % от цены», «15.01.2026»
        parts = tuple(int(p) for p in number.split("."))
        if hinted:
            heading = True  # стиль заголовка в DOCX — самый надёжный признак
        elif styled:
            heading = (
                len(rest) <= _TITLE_MAX
                and not _ends_sentence(rest, allowed=".")
                and (not rest.endswith(".") or _is_upper(rest) or len(rest) <= 90)
                and (not self.continues(i) or self.can_continue_title(i, i + 1))
            )
        else:
            heading = (
                len(rest) <= 80
                and not _ends_sentence(rest)
                and ". " not in rest  # внутри заголовка не кончаются предложения
                and self.opens_section(i)
            )
        if heading:
            title = rest.rstrip(".").strip()
            return _Candidate(
                SectionKind.HEADING,
                4 + len(parts),
                number,
                parts,
                title,
                f"{number}. {title}",
                styled,
            )
        return _Candidate(SectionKind.CLAUSE, 4 + len(parts), number, parts, "", number, styled)

    def _short_heading_ok(self, i: int, text: str) -> bool:
        if self.styled(i) or self.hint_level(i) is not None:
            return len(text) <= _TITLE_MAX
        return _heading_shaped(text) and self.opens_section(i)

    def _keyword(self, i: int) -> _Candidate | None:
        text = self.lines[i].text
        match = _KEYWORD.match(text)
        if not match or not text[0].isupper() or not self._short_heading_ok(i, text):
            return None
        rank = _KEYWORD_RANK[match["kw"].lower()]
        title = match["rest"].strip().rstrip(".")
        return _Candidate(SectionKind.PART, rank, None, None, title, text.rstrip("."), True)

    def _appendix(self, i: int) -> _Candidate | None:
        text = self.lines[i].text
        if not _APPENDIX.match(text) or not text[0].isupper():
            return None
        # настоящее приложение начинается с новой страницы или выделено оформлением
        near_top = i in self.first_on_page or (i - 1) in self.first_on_page
        if not near_top and not self.styled(i):
            return None
        if _ends_sentence(text) or len(text) > 120:
            return None
        return _Candidate(SectionKind.APPENDIX, _APPENDIX_RANK, None, None, text, text, True)

    def _typographic(self, i: int) -> _Candidate | None:
        line = self.lines[i]
        text = line.text
        upper = _is_upper(text)
        bold_or_big = line.bold or line.size >= self.doc.body_size + 1
        hinted = self.hint_level(i)
        if hinted is None and not (bold_or_big or (upper and len(text.split()) >= 2)):
            return None
        if len(_letters(text)) < 3 or len(text) > 150 or "___" in text or _ORG_FORM.match(text):
            return None
        if not text[0].isupper() and text[0] not in '«"(':
            return None
        if _ends_sentence(text, allowed="." if upper else ""):
            return None
        if hinted is not None:
            rank = _SECTION_RANK + hinted - 1
        elif line.size >= self.doc.body_size + 1 or upper:
            rank = _SECTION_RANK
        else:
            rank = -1.0  # жирный подзаголовок внутри текущего раздела — уточняется при сборке
        title = text.rstrip(".")
        return _Candidate(
            SectionKind.HEADING, rank, None, None, title, title, True, True, hinted is not None
        )

    def can_continue_title(self, first: int, j: int, untitled: bool = False) -> bool:
        """Строка j продолжает многострочный заголовок, начатый строкой first.

        `untitled` — у заголовка пока только номер («Статья 2»), следующая строка — его название.
        """
        if j >= len(self.lines):
            return False
        a, b = self.lines[first], self.lines[j]
        previous = self.lines[j - 1].text
        last_word = previous.rsplit(maxsplit=1)[-1].lower() if previous.split() else ""
        wrapped = (
            untitled
            or b.text[:1].islower()
            or (_is_upper(previous) and _is_upper(b.text))
            or last_word in _CONNECTORS
            or previous.endswith((",", "-", "—", "–"))
        )
        return (
            wrapped
            and b.page == a.page
            and self.alone[j]
            and b.bold == a.bold
            and abs(b.size - a.size) < 0.6
            and (a.bold or a.size >= self.doc.body_size + 1)
            and b.bbox[1] - self.lines[j - 1].bbox[3] < a.size * 1.2
            and not _NUMBERED.match(b.text)
            and not _KEYWORD.match(b.text)
            and not _APPENDIX.match(b.text)
        )


def _fits_numbering(parts: tuple[int, ...], open_numbers: list[tuple[int, ...]]) -> bool:
    """Номер продолжает нумерацию: первый вложенный пункт или следующий соседний."""
    if not open_numbers:
        return parts[-1] <= 3
    deepest = open_numbers[-1]
    if len(parts) == len(deepest) + 1 and parts[:-1] == deepest and parts[-1] <= 3:
        return True
    return any(
        len(number) == len(parts) and number[:-1] == parts[:-1] and 0 < parts[-1] - number[-1] <= 5
        for number in open_numbers
    )


@dataclass
class _Draft:
    candidate: _Candidate
    char_start: int
    parent: int | None
    level: int


def _collect(doc: ExtractedDocument, hints: Mapping[str, int]) -> tuple[str | None, list[_Draft]]:
    classifier = _Classifier(doc, hints, _alone_on_row(doc.lines))
    drafts: list[_Draft] = []
    stack: list[_Open] = []
    title: str | None = None
    i = 0
    while i < len(doc.lines):
        candidate = classifier.classify(i)
        if candidate is None:
            i += 1
            continue
        start, page = doc.lines[i].char_start, doc.lines[i].page
        i = _merge_title(classifier, candidate, i)
        if not drafts and page == 1:
            if candidate.kind == SectionKind.APPENDIX:
                continue  # «Приложение № 3 к извещению» над названием — пометка, а не раздел
            if candidate.typographic and not candidate.hinted and title is None:
                title = candidate.title  # первый заголовок документа — его название
                continue
        open_numbers = [o.parts for o in stack if o.parts is not None]
        unstyled_number = candidate.parts is not None and not candidate.styled
        if unstyled_number and not _fits_numbering(candidate.parts or (), open_numbers):
            continue
        if candidate.rank < 0:
            numbered = [o for o in stack if o.parts is not None]
            candidate.rank = numbered[-1].rank + 0.5 if numbered else _SECTION_RANK
        while stack and stack[-1].rank >= candidate.rank:
            stack.pop()
        parent = stack[-1].order if stack else None
        drafts.append(_Draft(candidate, start, parent, len(stack) + 1))
        stack.append(_Open(candidate.rank, len(drafts) - 1, candidate.parts))
    return title, drafts


def _merge_title(classifier: _Classifier, candidate: _Candidate, i: int) -> int:
    """Склеить многострочный заголовок. Возвращает индекс следующей необработанной строки."""
    j = i + 1
    if candidate.kind == SectionKind.CLAUSE:
        return j
    parts = [candidate.title] if candidate.title else []
    hinted = classifier.hint_level(i) is not None
    while (
        j - i < _MAX_TITLE_LINES
        and j < len(classifier.lines)
        and (
            classifier.hint_continues(i, j)
            if hinted
            else classifier.can_continue_title(i, j, untitled=not parts)
        )
        and len(" ".join(parts)) + len(classifier.lines[j].text) < _TITLE_MAX
    ):
        parts.append(classifier.lines[j].text.rstrip("."))
        j += 1
    if j > i + 1:
        merged = " ".join(parts)
        if candidate.title:
            prefix = candidate.label[: len(candidate.label) - len(candidate.title)]
            candidate.label = prefix + merged
        else:  # «Статья 5» + следующая строка «Цена контракта»
            candidate.label = f"{candidate.label}. {merged}"
        candidate.title = merged
    return j


def build_structure(
    doc: ExtractedDocument, root_label: str, hints: Mapping[str, int] | None = None
) -> DocumentStructure:
    """Дерево разделов. `hints` — заголовки из стилей DOCX: нормализованный текст → уровень."""
    title, drafts = _collect(doc, {_norm_key(k): v for k, v in (hints or {}).items()})
    text_end = len(doc.text)
    first_start = drafts[0].char_start if drafts else text_end
    if doc.text[:first_start].strip() or not drafts:
        label = "Вводная часть" if drafts else "Текст документа"
        preamble = _Candidate(SectionKind.PREAMBLE, _LEAF_RANK, None, None, label, label, True)
        drafts = [_Draft(preamble, 0, None, 1)] + [
            _Draft(d.candidate, d.char_start, None if d.parent is None else d.parent + 1, d.level)
            for d in drafts
        ]
    return DocumentStructure(title, tuple(_finalize(doc, drafts, root_label)))


def _subtree_ends(drafts: list[_Draft], text_end: int) -> list[int]:
    """Конец узла вместе с вложенными: начало следующего узла того же или более высокого уровня."""
    ends = [text_end] * len(drafts)
    open_nodes: list[int] = []
    for k, draft in enumerate(drafts):
        while open_nodes and drafts[open_nodes[-1]].level >= draft.level:
            ends[open_nodes.pop()] = draft.char_start
        open_nodes.append(k)
    return ends


def _finalize(doc: ExtractedDocument, drafts: list[_Draft], root_label: str) -> list[SectionNode]:
    nodes: list[SectionNode] = []
    text_end = len(doc.text)
    ends = _subtree_ends(drafts, text_end)
    for k, draft in enumerate(drafts):
        own_end = drafts[k + 1].char_start if k + 1 < len(drafts) else text_end
        char_end = ends[k]
        labels = [_short(draft.candidate.label)]
        parent = draft.parent
        while parent is not None:
            labels.append(_short(drafts[parent].candidate.label))
            parent = drafts[parent].parent
        last = max(char_end - 1, draft.char_start)
        nodes.append(
            SectionNode(
                order=k,
                parent=draft.parent,
                kind=draft.candidate.kind,
                number=draft.candidate.number,
                title=draft.candidate.title,
                label=draft.candidate.label,
                level=draft.level,
                path=PATH_SEPARATOR.join([root_label, *reversed(labels)]),
                char_start=draft.char_start,
                own_end=own_end,
                char_end=char_end,
                page_from=doc.page_at(draft.char_start),
                page_to=doc.page_at(last),
            )
        )
    return nodes
