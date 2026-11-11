"""Сводка портфеля не вызывает модель и не меняет результаты правил."""

from __future__ import annotations

from collections import Counter, defaultdict

from svetofor.db.enums import Light
from svetofor.schemas.portfolios import PortfolioFinding, PortfolioItemOut, RepeatedRisk


def overall_light(items: list[PortfolioItemOut]) -> Light:
    if any(item.light == Light.RED for item in items):
        return Light.RED
    if not items or any(item.status != "done" or item.light is None for item in items):
        return Light.GRAY
    if any(item.light == Light.GRAY for item in items):
        return Light.GRAY
    return Light.YELLOW if any(item.light == Light.YELLOW for item in items) else Light.GREEN


def repeated_risks(findings: list[PortfolioFinding]) -> list[RepeatedRisk]:
    checks: dict[str, set[str]] = defaultdict(set)
    titles: dict[str, str] = {}
    for finding in findings:
        checks[finding.rule_code].add(str(finding.check_id))
        titles[finding.rule_code] = finding.title
    return [
        RepeatedRisk(rule_code=code, title=titles[code], procurements=len(ids))
        for code, ids in sorted(checks.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        if len(ids) > 1
    ]


def counts(items: list[PortfolioItemOut]) -> dict[str, int]:
    counter = Counter(item.status for item in items)
    return {status: counter[status] for status in ("done", "running", "waiting", "failed")}
