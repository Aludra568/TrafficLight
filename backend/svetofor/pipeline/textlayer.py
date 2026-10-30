"""Качество текстового слоя PDF: «кракозябры» и мусор вместо текста.

Текстовый слой бывает испорчен: шрифт без таблицы Unicode даёт случайные символы, а русский
текст в однобайтовой кодировке, прочитанный как латиница, превращается в «Ñðîê ïîñòàâêè».
Такой текст выглядит как текст — в нём ничего не найдут ни грамматики, ни модель, и «условие
не найдено» окажется ложным. Поэтому:
- «кракозябры» cp1251 чинятся на месте — замена символ в символ, смещения не сдвигаются;
- страница, где по-прежнему мало похожих на слова токенов, отправляется на распознавание.
"""

from __future__ import annotations

import re

_WORD = re.compile(
    r"^[«(\"'„]*(?:[А-ЯЁа-яёA-Za-z]{2,}(?:-[А-ЯЁа-яёA-Za-z]+)*|[вскоиауяжВСКОИАУЯЖ])"
    r"[»)\"'.,;:!?…]*$"
)  # однобуквенные слова русского языка («в», «с», «к», «и»…) — тоже слова, не мусор
_NUMBER = re.compile(r"^[№(]*\d[\d.,%/:\-–]*[).,;:]*$")
_LATIN1_LETTER = re.compile(r"[À-ÿ]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LETTER = re.compile(r"[^\W\d_]")
MIN_TOKENS = 30  # меньше — слишком мало текста, чтобы судить (схема, подписи, штамп)
GARBAGE_BELOW = 0.5


def text_quality(text: str, min_tokens: int = 5) -> float | None:
    """Доля «похожих на слова» токенов, 0..1. None — текста слишком мало для оценки."""
    tokens = text.split()
    if len(tokens) < min_tokens:
        return None
    good = sum(1 for t in tokens if _WORD.match(t) or _NUMBER.match(t))
    return round(good / len(tokens), 3)


def is_mojibake(text: str) -> bool:
    """Русский текст в cp1251, прочитанный как Latin-1: буквы «À–ÿ» и ни одной кириллицы."""
    letters = _LETTER.findall(text)
    if len(letters) < 20 or _CYRILLIC.search(text):
        return False
    return len(_LATIN1_LETTER.findall(text)) > 0.6 * len(letters)


def _repair_char(ch: str) -> str:
    if ord(ch) > 0xFF:
        return ch
    try:
        return ch.encode("latin-1").decode("cp1251")
    except UnicodeDecodeError:  # байт 0x98 в cp1251 не определён
        return ch


def repair_mojibake(text: str) -> str:
    """«Ñðîê ïîñòàâêè» → «Срок поставки». Символ в символ: длина и смещения те же."""
    return "".join(_repair_char(ch) for ch in text)


def is_garbage(text: str) -> bool:
    """Текст есть, но почти не похож на слова — шрифт без таблицы Unicode или мусор."""
    quality = text_quality(text, MIN_TOKENS)
    return quality is not None and quality < GARBAGE_BELOW
