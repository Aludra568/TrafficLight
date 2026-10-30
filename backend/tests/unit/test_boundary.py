"""Набор «граница GREEN»: детерминированные замеры не плывут.

Набор проверяет архитектуру без знания формулировок риска, поэтому первый тест — что словарь не
подогнан под набор: ни грамматики, ни тревожные формулировки не должны «знать» эти риски заранее.
"""

from __future__ import annotations

from pathlib import Path

from svetofor.eval.boundary import load_cases, measure_all, unparsed_values
from svetofor.rules.loader import load_ruleset_file

RULES = load_ruleset_file(
    Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
).rules
ROWS = {row["id"]: row for row in measure_all(RULES)}


def test_set_shape() -> None:
    cases = load_cases()
    assert len([c for c in cases if c.present]) == 50
    assert len([c for c in cases if not c.present]) == 15


def test_vocabulary_not_tuned_to_the_set() -> None:
    """Тревожные формулировки и грамматики не знают рисков набора — иначе он ничего не мерит."""
    risky = [row for row in ROWS.values() if row["present"]]
    assert not [row["id"] for row in risky if row["red_flag"]]
    assert all(
        not row["grammar"] or row["id"] == "B38" for row in risky
    )  # B38: нашлись только 7 дн.


def test_value_coverage_prototype() -> None:
    """«Все сроки и доли в предложениях о поле разобраны»: 9 из 12 рисков со значениями видны
    без модели; законные формулировки — без ложных тревог."""
    value_rules = {"PAY-01", "ACC-01", "TERM-01", "SEC-01"}
    caught = {row["id"] for row in ROWS.values() if row["present"] and row["unparsed"]}
    assert caught == {"B36", "B37", "B38", "B39", "B42", "B43", "B44", "B45", "B47"}
    clean = [row for row in ROWS.values() if not row["present"] and row["rule"] in value_rules]
    assert not [row["id"] for row in clean if row["unparsed"]]


def test_reference_point_is_not_topic() -> None:
    """«с даты поставки», «просрочке поставки» — точка отсчёта, а не тема: не ложная тревога."""
    assert unparsed_values("Счет выставляется в течение 3 рабочих дней с даты поставки.") == {}
    assert unparsed_values("Отказ при однократной просрочке поставки на один день.") == {}


def test_item_sweep_coverage() -> None:
    """Поштучная проверка: 47 рисков видны в одном пункте, ещё 2 — после подстановки текста по
    ссылке «в пункте 8.2»; B44 — смысл связан без ссылки (остаточный класс)."""
    risky = [row for row in ROWS.values() if row["present"]]
    assert sum(row["self_contained"] for row in risky) == 47
    resolved = {
        row["id"] for row in risky if not row["self_contained"] and row["references_resolve"]
    }
    assert resolved == {"B18", "B40"}
