"""Нацрежим (ПП № 1875) по предложению извещения: запрет, ограничение, преимущество или «нет»."""

from __future__ import annotations

import re

_MEASURES = (("запрет", "ban"), ("ограничени", "restriction"), ("преимуществ", "advantage"))


def national_regime(match: re.Match[str]) -> tuple[str, None] | None:
    sentence = match.group(0)
    if re.search(r"не\s+установлен", sentence):
        return ("none", None)
    measures = [value for word, value in _MEASURES if word in sentence]
    # «основанием для установки запретов, ограничений … а также преимуществ» — ссылка на закон
    # в печатной форме ЕИС, а не установленная мера (31.10.2026: 4 ложных SUB-02 из 8 пакетов)
    return (measures[0], None) if len(measures) == 1 else None
