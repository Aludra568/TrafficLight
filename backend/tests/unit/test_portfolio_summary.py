"""Ожидаемые итоги маленького портфеля заданы критериями A3/A4, не кодом агрегатора."""

import uuid

import pytest

from svetofor.db.enums import Light
from svetofor.portfolios.summary import overall_light, repeated_risks
from svetofor.schemas.portfolios import PortfolioFinding, PortfolioItemOut


def item(light: str | None, status: str = "done") -> PortfolioItemOut:
    return PortfolioItemOut(
        id=uuid.uuid4(),
        position=0,
        source_type="file",
        source_name="Закупка",
        status=status,
        light=light,
    )


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([], Light.GRAY),
        ([item("green")], Light.GREEN),
        ([item("green"), item("yellow")], Light.YELLOW),
        ([item("green"), item(None, "running")], Light.GRAY),
        ([item("yellow"), item("gray")], Light.GRAY),
        ([item("yellow"), item(None, "failed")], Light.GRAY),
        ([item("red"), item(None, "running")], Light.RED),
        ([item("red"), item(None, "failed")], Light.RED),
    ],
)
def test_light_never_hides_known_risk_or_incomplete_checks(items, expected) -> None:
    assert overall_light(items) == expected


def test_repeated_rule_counts_procurements_not_quotes() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    findings = [
        PortfolioFinding(
            id=uuid.uuid4(),
            check_id=check,
            source_name="Закупка",
            rule_code=code,
            title="Риск",
            severity="red",
            category="payment",
        )
        for check, code in [
            (first, "PAY-01"),
            (first, "PAY-01"),
            (second, "PAY-01"),
            (first, "ONE-01"),
        ]
    ]
    result = repeated_risks(findings)
    assert [(row.rule_code, row.procurements) for row in result] == [("PAY-01", 2)]
