"""Числительные прописью: «десяти», «двадцати пяти», «тридцать (30)».

Нужны в двух местах: грамматики карточки читают сроки прописью, а проверка фактов сверяет число
модели с прописью в цитате. В русских договорах число почти всегда дублируется прописью —
«30 (тридцать) дней»; расхождение цифр и прописи выдаёт ошибку распознавания или опечатку
(как сверка суммы цифрами и прописью в чеках).

Текст на входе — нормализованный (`verify.normalize`): строчные, «ё» → «е».
"""

from __future__ import annotations

import re

NUMERALS: dict[str, int] = {
    **dict.fromkeys(("один", "одного", "одной", "одну", "одна", "одних"), 1),
    **dict.fromkeys(("два", "две", "двух"), 2),
    **dict.fromkeys(("три", "трех"), 3),
    **dict.fromkeys(("четыре", "четырех"), 4),
    **dict.fromkeys(("пять", "пяти"), 5),
    **dict.fromkeys(("шесть", "шести"), 6),
    **dict.fromkeys(("семь", "семи"), 7),
    **dict.fromkeys(("восемь", "восьми"), 8),
    **dict.fromkeys(("девять", "девяти"), 9),
    **dict.fromkeys(("десять", "десяти"), 10),
    **dict.fromkeys(("одиннадцать", "одиннадцати"), 11),
    **dict.fromkeys(("двенадцать", "двенадцати"), 12),
    **dict.fromkeys(("тринадцать", "тринадцати"), 13),
    **dict.fromkeys(("четырнадцать", "четырнадцати"), 14),
    **dict.fromkeys(("пятнадцать", "пятнадцати"), 15),
    **dict.fromkeys(("шестнадцать", "шестнадцати"), 16),
    **dict.fromkeys(("семнадцать", "семнадцати"), 17),
    **dict.fromkeys(("восемнадцать", "восемнадцати"), 18),
    **dict.fromkeys(("девятнадцать", "девятнадцати"), 19),
    **dict.fromkeys(("двадцать", "двадцати"), 20),
    **dict.fromkeys(("тридцать", "тридцати"), 30),
    **dict.fromkeys(("сорок", "сорока"), 40),
    **dict.fromkeys(("пятьдесят", "пятидесяти"), 50),
    **dict.fromkeys(("шестьдесят", "шестидесяти"), 60),
    **dict.fromkeys(("семьдесят", "семидесяти"), 70),
    **dict.fromkeys(("восемьдесят", "восьмидесяти"), 80),
    **dict.fromkeys(("девяносто", "девяноста"), 90),
    **dict.fromkeys(("сто", "ста"), 100),
}
NUMERAL = "|".join(sorted(NUMERALS, key=len, reverse=True))
# одно число прописью — до трёх слов подряд («сто двадцать пять»)
_SPELLED = re.compile(rf"(?<![а-я])(?:{NUMERAL})(?:\s+(?:{NUMERAL})){{0,2}}(?![а-я])")


def parse_numeral(words: str) -> int | None:
    """«двадцати пяти» → 25, «сто двадцать» → 120. Порядок разрядов нарушен — None."""
    values = [NUMERALS.get(w) for w in words.split()]
    if not values or any(v is None for v in values):
        return None
    total, last = 0, 1000
    for value in values:
        assert value is not None
        place = 100 if value >= 100 else 10 if value >= 10 else 1
        if place >= last or (place == 10 and value < 20 and last < 1000 and last != 100):
            return None  # «пять двадцать», «двадцать одиннадцать»
        total += value
        last = place if value >= 20 or value < 10 else 1  # «одиннадцать» закрывает и единицы
    return total


def spelled_numbers(normalized: str) -> list[int]:
    """Все числа прописью в нормализованном тексте, по порядку."""
    found = (parse_numeral(m.group(0)) for m in _SPELLED.finditer(normalized))
    return [n for n in found if n is not None]
