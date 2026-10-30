"""Светофор документа и пакета."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from svetofor.db.enums import Light, RuleOutcome, Severity
from svetofor.rules.model import Rule, ThresholdRule

_RANK = {Light.GREEN: 0, Light.YELLOW: 1, Light.RED: 2}

# Зелёный — утверждение «рисков нет», поэтому его нельзя выдать, если проверена меньшая часть правил
MIN_COVERAGE = 0.5


def light_from_severities(severities: Iterable[Severity]) -> Light:
    """Красный, если есть хоть одно критичное; жёлтый, если есть «внимание»; иначе зелёный."""
    result = Light.GREEN
    for severity in severities:
        if severity is Severity.RED:
            return Light.RED
        result = Light.YELLOW
    return result


def worst(lights: Iterable[Light]) -> Light:
    return max(lights, key=_RANK.__getitem__, default=Light.GREEN)


def package_light(
    document_lights: Iterable[Light], package_level: Iterable[Severity] = ()
) -> Light:
    """Пакет: худший цвет документов и замечаний уровня пакета (противоречия между документами)."""
    return worst([*document_lights, light_from_severities(package_level)])


def package_verdict(
    document_lights: Iterable[Light],
    package_level: Iterable[Severity],
    assessed: int,
    total: int,
    critical_gaps: int = 0,
    incomplete_documents: int = 0,
) -> Light:
    """Цвет пакета с учётом полноты проверки.

    Найденный риск остаётся риском при любой полноте. А «зелёный» без замечаний превращается
    в серый («риски не оценены»), если:
    - правил с исходом по существу (нарушение, в порядке, отсутствует) меньше половины: не
      открылись документы, недоступна модель, набор правил для другой процедуры;
    - или не проверено из-за сбоя хотя бы одно правило, которое может дать «критично»
      (`critical_gaps`): модель недоступна, текст не прочитан, документ даёт указания
      программе проверки. Доля тут не важна — неустойка и односторонний отказ дороже,
      чем 25 проверенных «мелочей»;
    - или текст пакета прочитан не полностью (`incomplete_documents`): страницы не распознаны,
      документ не открылся или пропущен при распаковке (пароль, повреждение). Доказанные
      «в порядке» по прочитанному не говорят ничего о непрочитанном.
    """
    light = package_light(document_lights, package_level)
    if light is Light.GREEN and (
        total == 0
        or assessed < total * MIN_COVERAGE
        or critical_gaps > 0
        or incomplete_documents > 0
    ):
        return Light.GRAY
    return light


def can_be_red(rule: Rule) -> bool:
    """Может ли правило дать «критично» — по цвету, условиям порога или «условие не найдено»."""
    severities = {getattr(rule, "severity", None), getattr(rule, "absent_severity", None)}
    if isinstance(rule, ThresholdRule):
        severities |= {c.severity for c in rule.conditions}
        if rule.on_missing is not None:
            severities.add(rule.on_missing.severity)
    return Severity.RED in severities


def critical_unchecked(
    results: Iterable[tuple[str, RuleOutcome, dict[str, Any] | None]], red_capable: set[str]
) -> list[str]:
    """Правила «критично», исход которых неизвестен (не проверены из-за сбоя, блокировки или
    отсутствия документа), — каждое делает зелёный невозможным."""
    return sorted(
        code
        for code, outcome, details in results
        if outcome is RuleOutcome.NOT_CHECKED and code in red_capable and is_failure(details)
    )


def is_failure(details: dict[str, Any] | None) -> bool:
    """«Не проверено» из-за сбоя (модель, карточка, нечитаемый текст, указания программе) или
    потому что нужного правилу документа нет в пакете вовсе, а не по существу (другой закон,
    значение не указано). Для правила «критично» такой пробел делает зелёный невозможным."""
    details = details or {}
    return bool(details.get("error") or details.get("blocked") or details.get("missing_documents"))
