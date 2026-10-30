"""Безопасная подстановка значений в тексты правил: «Оплата через {value} раб. дн.».

Шаблоны пишет методолог, поэтому разрешены только простые имена ({value}, {spec}) — без доступа
к атрибутам и индексам ({value.__class__}, {x[0]}) и без спецификаторов формата.
"""

from __future__ import annotations

import string

_FORMATTER = string.Formatter()


class TemplateError(ValueError):
    pass


def template_fields(template: str) -> set[str]:
    fields: set[str] = set()
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise TemplateError(f"Ошибка в шаблоне текста: {exc}") from exc
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise TemplateError(
                "В шаблоне можно использовать только простые подстановки вида {value}, "
                f"а не «{{{name}}}»"
            )
        fields.add(name)
    return fields


def render(template: str, **values: str) -> str:
    """Подставить значения. Неизвестные поля остаются как есть, а не роняют проверку."""
    template_fields(template)
    out: list[str] = []
    for literal, name, _, _ in _FORMATTER.parse(template):
        out.append(literal)
        if name is not None:
            out.append(values.get(name, "{" + name + "}"))
    return "".join(out)
