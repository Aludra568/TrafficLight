"""Закупки по 223-ФЗ: свои нормы и полнота проверки.

До изменения к 223-ФЗ относились 9 правил из 31, а неприменимые правила 44-ФЗ входили в
знаменатель полноты: полнота не выше 29 % при пороге 50 % — чистый пакет 223-ФЗ всегда был
серым «не оценено». Теперь правила другого закона в полноту не входят, а для 223-ФЗ есть
свои нормы (оплата и обеспечение для МСП — ПП № 1352, односторонний отказ и неустойка — ГК РФ).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import Light, RuleOutcome
from tests.integration.test_redteam_green import CLEAN_SPEC, HonestModel, pdf, run

pytestmark = pytest.mark.pg

TERMINATION_OK = (
    "Заказчик вправе отказаться от исполнения договора в одностороннем порядке только при "
    "существенном нарушении договора поставщиком"
)
PENALTY_CAP_OK = "Общий размер неустойки не может превышать 10 % цены договора"
CONTRACT_223 = f"""
!! ПРОЕКТ ДОГОВОРА
Договор заключается в соответствии с Федеральным законом № 223-ФЗ.
**1. ПРЕДМЕТ ДОГОВОРА**
1.1. Поставщик обязуется поставить бумагу офисную в количестве 500 пачек.
**2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ**
2.1. Заказчик выплачивает аванс в размере 30 % цены договора.
2.2. Оплата производится в течение {{payment}} рабочих дней с даты подписания документа о приемке.
**3. СРОКИ ПОСТАВКИ**
3.1. Срок поставки — в течение 15 рабочих дней с даты заключения договора.
**4. ПРИЕМКА**
4.1. Приемка товара осуществляется в течение 5 рабочих дней с даты поставки.
**5. ОБЕСПЕЧЕНИЕ**
5.1. Обеспечение исполнения договора устанавливается в размере 5 % цены договора.
**6. ОТВЕТСТВЕННОСТЬ СТОРОН**
6.1. Пеня начисляется в размере одной трехсотой ключевой ставки Центрального банка.
6.2. Заказчик несет ответственность за просрочку оплаты в виде пени.
6.3. {PENALTY_CAP_OK}.
**7. РАСТОРЖЕНИЕ ДОГОВОРА**
7.1. {TERMINATION_OK}.
"""
NOTICE_223 = """
!! ИЗВЕЩЕНИЕ О ПРОВЕДЕНИИ ЗАПРОСА КОТИРОВОК
**1. Общие сведения**
1.1. Закупка осуществляется в соответствии с Федеральным законом № 223-ФЗ.
1.2. Размер обеспечения исполнения договора — 5 % цены договора.
"""
OKS = {"TRM-21": TERMINATION_OK, "LIA-21": PENALTY_CAP_OK}


def _files(payment: int = 7) -> dict[str, object]:
    return {
        "Проект договора.pdf": pdf(CONTRACT_223.replace("{payment}", str(payment))),
        "Техническое задание.pdf": pdf(CLEAN_SPEC),
        "Извещение.pdf": pdf(NOTICE_223),
    }


def test_clean_223_package_can_be_green(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    light, results, _ = run(session_factory, tmp_path, _files(), HonestModel(oks=OKS))  # type: ignore[arg-type]
    other_law = [c for c, r in results.items() if (r.details or {}).get("other_law")]
    assert "PAY-01" in other_law and "LIA-01" in other_law  # нормы 44-ФЗ не применяются
    assert results["PAY-21"].outcome is RuleOutcome.OK
    assert results["TRM-21"].outcome is RuleOutcome.OK
    assert light is Light.GREEN, {
        c: (r.outcome.value, r.reason) for c, r in results.items() if r.outcome.value != "ok"
    }


def test_long_payment_in_223_contract_is_found(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    light, results, findings = run(
        session_factory,
        tmp_path,
        _files(payment=20),  # type: ignore[arg-type]
        HonestModel(oks=OKS),
    )
    assert results["PAY-21"].outcome is RuleOutcome.VIOLATION
    assert any(f.rule_code == "PAY-21" for f in findings)
    assert light is Light.YELLOW
