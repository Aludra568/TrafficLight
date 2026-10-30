"""Каталог фактов карточки закупки — единый источник правды.

Используется: валидацией правил (threshold, cross_document), промптами извлечения, интерфейсом
(подписи), подстановкой {value} в сообщения правил.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from svetofor.db.enums import DocType


class FactKind(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    BOOL = "bool"
    ENUM = "enum"


@dataclass(frozen=True)
class FactSpec:
    key: str
    label: str  # подпись в интерфейсе
    kind: FactKind
    unit: str | None = None
    choices: dict[str, str] = field(default_factory=dict)  # значение → подпись (для ENUM)
    hint: str = ""  # подсказка модели при извлечении
    sources: tuple[DocType, ...] = (DocType.NOTICE, DocType.SPEC, DocType.CONTRACT)


_FACTS: tuple[FactSpec, ...] = (
    FactSpec(
        "law",
        "Закон",
        FactKind.ENUM,
        choices={"44-ФЗ": "44-ФЗ", "223-ФЗ": "223-ФЗ", "commercial": "коммерческая закупка"},
        hint="по какому закону проводится закупка",
        sources=(DocType.NOTICE, DocType.CONTRACT),
    ),
    FactSpec(
        "procurement_number", "Номер закупки", FactKind.TEXT, hint="реестровый номер извещения"
    ),
    FactSpec("subject", "Предмет закупки", FactKind.TEXT, hint="что закупается, кратко"),
    FactSpec(
        "application_deadline",
        "Окончание подачи заявок",
        FactKind.TEXT,
        hint="дата и время окончания срока подачи заявок в виде ДД.ММ.ГГГГ ЧЧ:ММ",
        sources=(DocType.NOTICE,),
    ),
    FactSpec(
        "nmck_rub", "НМЦК", FactKind.NUMBER, "₽", hint="начальная (максимальная) цена в рублях"
    ),
    FactSpec(
        "delivery_term_days",
        "Срок поставки",
        FactKind.NUMBER,
        "дн.",
        hint="срок поставки или выполнения в днях; укажи unit: working или calendar",
    ),
    FactSpec(
        "payment_term_working_days",
        "Срок оплаты",
        FactKind.NUMBER,
        "раб. дн.",
        hint="через сколько рабочих дней после подписания документа о приёмке производится оплата",
        sources=(DocType.CONTRACT,),
    ),
    FactSpec(
        "acceptance_term_working_days",
        "Срок приёмки",
        FactKind.NUMBER,
        "раб. дн.",
        hint="срок приёмки товара (работ, услуг) в рабочих днях",
        sources=(DocType.CONTRACT,),
    ),
    FactSpec(
        "advance_percent",
        "Аванс",
        FactKind.NUMBER,
        "%",
        hint="размер аванса в процентах от цены; 0, если аванс не предусмотрен",
        sources=(DocType.CONTRACT,),
    ),
    FactSpec(
        "bid_security_percent",
        "Обеспечение заявки",
        FactKind.NUMBER,
        "%",
        hint="размер обеспечения заявки в процентах от начальной (максимальной) цены",
        sources=(DocType.NOTICE,),
    ),
    FactSpec(
        "contract_security_percent",
        "Обеспечение исполнения",
        FactKind.NUMBER,
        "%",
        hint="размер обеспечения исполнения контракта в процентах",
        sources=(DocType.NOTICE, DocType.CONTRACT),
    ),
    FactSpec(
        "warranty_months",
        "Гарантийный срок",
        FactKind.NUMBER,
        "мес.",
        hint="гарантийный срок в месяцах",
    ),
    FactSpec(
        "warranty_security_percent",
        "Обеспечение гарантийных обязательств",
        FactKind.NUMBER,
        "%",
        hint="размер обеспечения гарантийных обязательств в процентах от цены контракта",
        sources=(DocType.NOTICE, DocType.CONTRACT),
    ),
    FactSpec(
        "smp_only",
        "Только для СМП и СОНКО",
        FactKind.BOOL,
        hint="закупка только среди субъектов малого предпринимательства",
        sources=(DocType.NOTICE,),
    ),
    FactSpec(
        "national_regime",
        "Нацрежим (ПП № 1875)",
        FactKind.ENUM,
        choices={
            "ban": "запрет",
            "restriction": "ограничение",
            "advantage": "преимущество",
            "none": "нет",
        },
        hint="какая мера национального режима установлена",
        sources=(DocType.NOTICE, DocType.SPEC),
    ),
    FactSpec("delivery_place", "Место поставки", FactKind.TEXT, hint="адрес или адреса поставки"),
    FactSpec(
        "penalty_rate",
        "Пеня поставщика",
        FactKind.TEXT,
        hint="размер пени за просрочку, как написано",
        sources=(DocType.CONTRACT,),
    ),
    FactSpec(
        "quantity_total",
        "Количество",
        FactKind.NUMBER,
        hint="общее количество товара; unit — единица измерения как в документе",
        sources=(DocType.SPEC, DocType.CONTRACT),
    ),
)

FACTS: dict[str, FactSpec] = {f.key: f for f in _FACTS}


def display_value(key: str, value: object) -> str:
    """Человекочитаемое значение факта для сообщений и интерфейса."""
    spec = FACTS.get(key)
    if value is None:
        return "—"
    if spec is not None and spec.kind is FactKind.ENUM:
        return spec.choices.get(str(value), str(value))
    if spec is not None and spec.kind is FactKind.BOOL:
        return "да" if value else "нет"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return format_number(value, money=spec is not None and spec.unit == "₽")
    return str(value)


def format_number(value: float, money: bool = False) -> str:
    """По-русски: разряды через неразрывный пробел, дробь через запятую; рубли — с копейками.
    1890000.0 → «1 890 000,00» (было «1,89e+06»), 0.5 → «0,5», 30.0 → «30»."""
    text = f"{value:,.2f}" if money else f"{value:,.4f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")
