"""Проверка цитат — главный механизм «верности».

Цитата модели принимается, только если она реально есть в документе. В отчёт идёт текст документа
(с точными смещениями), а не текст модели. Нечёткое сравнение нужно для OCR-опечаток и переносов,
но оно не имеет права «согласиться» с изменённым смыслом, поэтому дополнительно проверяется:
- все числа в найденном фрагменте совпадают с числами цитаты;
- совпадает количество отрицаний (не / ни / без);
- найдена почти вся цитата, а не её кусок.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz


def is_invisible(ch: str) -> bool:
    """Невидимый символ: формат Unicode (Cf — мягкий перенос, нулевые пробелы, управление
    направлением письма, теги U+E0000…) и селекторы вариантов. Человек их не видит, а шаблоны
    и поиск спотыкаются («банков⁠ская»), модель же читает теги как текст (ASCII smuggling)."""
    code = ord(ch)
    return (
        unicodedata.category(ch) == "Cf" or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF
    )


def strip_invisible(text: str) -> str:
    return "".join(ch for ch in text if not is_invisible(ch))


_QUOTES = set("«»“”„‟\"'’‘`´‹›")
_DASHES = set("‐‑‒–—―−")
_HYPHENATION = re.compile(r"(?<=[^\W\d_])-[ \t]*\r?\n[ \t]*(?=[a-zа-яё])")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
MAX_ELLIPSIS_GAP = 300  # символов между кусками цитаты «А … Б»
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION = re.compile(r"(?<!\w)(?:не|ни|без)(?!\w)")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN = re.compile(r"[A-Za-z]")
# знак номера после OCR: «No 1042», «N° 1042», «Nо» с русской «о»; только перед числом и не
# внутри слова — «Nokia 5» и «No.» в английском тексте не трогаем
_NUMBER_SIGN = re.compile(r"(?<![A-Za-zА-Яа-яЁё])[NН][oо°º](?=\s?\d)")
# латинские буквы, неотличимые от русских на экране (замена один к одному — смещения не сдвигаются)
_LOOKALIKES = str.maketrans("AaBCcEeHKkMOoPpTXxYy", "АаВСсЕеНКкМОоРрТХхУу")


def fold_lookalikes(text: str) -> str:
    """«Срок пoставки» с латинской «o» → «Срок поставки».

    Только в словах, где есть и кириллица, и латиница: «ГОСТ R», «Wi-Fi» и «iPhone» не трогаем.
    Подмена букв — известный способ спрятать закупку от поиска; без сворачивания она ломала бы
    и проверку цитат, и грамматики карточки, и полнотекстовый поиск (`text_norm`).
    """

    def fold(match: re.Match[str]) -> str:
        word = match.group(0)
        if _CYRILLIC.search(word) and _LATIN.search(word):
            return word.translate(_LOOKALIKES)
        return word

    return _WORD.sub(fold, text)


@dataclass(frozen=True)
class NormalizedText:
    original: str
    text: str
    index_map: tuple[int, ...]  # позиция в text → позиция в original

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        """Смещения в исходном тексте для отрезка [start, end) нормализованного."""
        if start >= end:
            raise ValueError("пустой отрезок")
        return self.index_map[start], self.index_map[end - 1] + 1


def normalize_with_map(original: str) -> NormalizedText:
    skip: set[int] = set()
    for match in _HYPHENATION.finditer(original):
        skip.update(range(match.start(), match.end()))
    # «No 1042» → «№ 1042»: Tesseract распознаёт знак номера как «No» (замер OCR, 2026-10-07),
    # а правила, грамматики и цитаты пишут «№». «No» — один «№» на месте «N», «o» пропускается
    number_signs: set[int] = set()
    for match in _NUMBER_SIGN.finditer(original):
        number_signs.add(match.start())
        skip.update(range(match.start() + 1, match.end()))

    folded = fold_lookalikes(original)  # та же длина, что и original
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(folded):
        if i in skip or is_invisible(ch):
            continue
        if ch.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                index_map.append(i)
            continue
        if i in number_signs:
            out = "№"
        elif ch in _QUOTES:
            out = '"'
        elif ch in _DASHES:
            out = "-"
        else:
            out = ch.lower().replace("ё", "е")
        for c in out:
            chars.append(c)
            index_map.append(i)
    if chars and chars[-1] == " ":
        chars.pop()
        index_map.pop()
    return NormalizedText(original=original, text="".join(chars), index_map=tuple(index_map))


def normalize(text: str) -> str:
    return normalize_with_map(text).text


@dataclass(frozen=True)
class TextSource:
    id: str
    text: str


@dataclass(frozen=True)
class QuoteMatch:
    source_id: str
    score: float  # 0–100; 100 — точное совпадение после нормализации
    start: int  # смещения в исходном тексте источника
    end: int
    text: str  # точный текст из документа — именно он идёт в отчёт
    exact: bool


@dataclass(frozen=True)
class _SegmentHit:
    start: int  # в нормализованном тексте источника
    end: int
    score: float


def _numbers(text: str) -> list[str]:
    return [n.replace(",", ".") for n in _NUMBER.findall(text)]


# Модальность: «обязан» ↔ «вправе» — одно слово, нечёткое сравнение его пропускает (95 из 100),
# а смысл обратный: «Заказчик вправе произвести оплату» — оплата не обязательна. Разведка 3, Р2:
# подмена модальности — известный класс искажённых цитат в юридических ИИ-системах
_MODALITY = (
    ("duty", re.compile(r"(?<!\w)(?:обязан\w*|обязу\w*|долж(?:ен|на|но|ны))(?!\w)")),
    ("right", re.compile(r"(?<!\w)(?:вправе|мож(?:ет|ут|но)|име(?:ет|ют)\s+право)(?!\w)")),
    ("ban", re.compile(r"(?<!\w)(?:запреща\w*|недопустим\w*)(?!\w)")),
)


def _modality(text: str) -> list[str]:
    found = [(m.start(), kind) for kind, pattern in _MODALITY for m in pattern.finditer(text)]
    return [kind for _, kind in sorted(found)]


def _same_meaning_markers(quote_norm: str, found_norm: str) -> bool:
    """Числа, отрицания и модальность цитаты и найденного текста совпадают."""
    return (
        _numbers(quote_norm) == _numbers(found_norm)
        and len(_NEGATION.findall(quote_norm)) == len(_NEGATION.findall(found_norm))
        and _modality(quote_norm) == _modality(found_norm)
    )


_NEGATION_BEFORE = re.compile(r"(?<!\w)(?:не|ни)\s?$")


def _cut_after_negation(source_norm: str, start: int, first_segment: str) -> bool:
    """Цитата начинается сразу после «не»: «несет ответственность» из «не несет ответственность».

    Точная подстрока, но смысл обратный — такая цитата ничего не доказывает.
    """
    return bool(_NEGATION_BEFORE.search(source_norm[max(0, start - 4) : start])) and not (
        _NEGATION.match(first_segment)
    )


# слово модальности прямо перед цитатой: «…вправе |произвести оплату…»
_MODAL_BEFORE = re.compile("(?:" + "|".join(p.pattern for _, p in _MODALITY) + r")\s?$")


def _cut_after_modality(source_norm: str, start: int, first_segment: str) -> bool:
    """Цитата начинается сразу после «вправе» / «обязан»: «произвести оплату в течение 7 дней»
    из «Заказчик вправе произвести оплату…». Точная подстрока, модальности в ней нет — и сверка
    модальности её не видит, а смысл («оплата не обязательна») потерян (код-ревью 2026-10-08)."""
    before = source_norm[max(0, start - 30) : start]
    return bool(_MODAL_BEFORE.search(before)) and not _modality(first_segment[:30])


def _gap_keeps_meaning(gap: str, strict: bool) -> bool:
    """Пропуск «А … Б» — внутри одной мысли: не длиннее `MAX_ELLIPSIS_GAP`, без отрицания
    («Заказчик … вправе» из «Заказчик не вправе»); для «в порядке» (`strict`) — и без чисел
    («Пеня … цены» из «Пеня 1/100 цены»): доказательство отсутствия риска не прячет главное."""
    if len(gap) > MAX_ELLIPSIS_GAP or _NEGATION.search(gap):
        return False
    return not (strict and _NUMBER.search(gap))


def _expand_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    return start, end


class QuoteVerifier:
    def __init__(
        self, min_score: int = 92, min_length: int = 12, min_coverage: float = 0.85
    ) -> None:
        self.min_score = min_score
        self.min_length = min_length
        self.min_coverage = min_coverage

    def _find_segment(self, segment: str, source: str, offset: int) -> _SegmentHit | None:
        exact = source.find(segment, offset)
        if exact >= 0:
            return _SegmentHit(exact, exact + len(segment), 100.0)
        haystack = source[offset:]
        if len(segment) > len(haystack):
            return None
        alignment = fuzz.partial_ratio_alignment(segment, haystack, score_cutoff=self.min_score)
        if alignment is None:
            return None
        start, end = _expand_to_words(haystack, alignment.dest_start, alignment.dest_end)
        if (end - start) < self.min_coverage * len(segment):
            return None
        if not _same_meaning_markers(segment, haystack[start:end]):
            return None
        return _SegmentHit(offset + start, offset + end, float(alignment.score))

    def _match_in(
        self, segments: list[str], source: TextSource, strict: bool = False
    ) -> QuoteMatch | None:
        norm = normalize_with_map(source.text)
        offset = 0
        hits: list[_SegmentHit] = []
        for segment in segments:
            hit = self._find_segment(segment, norm.text, offset)
            if hit is None:
                return None
            if hits and not _gap_keeps_meaning(norm.text[hits[-1].end : hit.start], strict):
                return None
            hits.append(hit)
            offset = hit.end
        if _cut_after_negation(norm.text, hits[0].start, segments[0]) or _cut_after_modality(
            norm.text, hits[0].start, segments[0]
        ):
            return None
        start, end = norm.original_span(hits[0].start, hits[-1].end)
        score = min(h.score for h in hits)
        return QuoteMatch(
            source_id=source.id,
            score=score,
            start=start,
            end=end,
            text=source.text[start:end],
            exact=score == 100.0,
        )

    def segments(self, quote: str) -> list[str]:
        parts = [normalize(p) for p in _ELLIPSIS.split(quote)]
        return [p for p in parts if len(p) >= 4]

    def verify(
        self,
        quote: str | None,
        sources: Sequence[TextSource],
        preferred_id: str | None = None,
        strict: bool = False,
    ) -> QuoteMatch | None:
        """Найти цитату в источниках, начиная с указанного моделью. None — не подтверждена.

        `strict` — цитата доказывает «в порядке»: пропуск «…» не может скрывать числа."""
        if not quote:
            return None
        segments = self.segments(quote)
        if not segments or sum(len(s) for s in segments) < self.min_length:
            return None
        ordered = sorted(sources, key=lambda s: s.id != preferred_id)
        best: QuoteMatch | None = None
        for source in ordered:
            match = self._match_in(segments, source, strict)
            if match is None:
                continue
            if match.exact:
                return match
            if best is None or match.score > best.score:
                best = match
        return best
