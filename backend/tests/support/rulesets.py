"""Стартовый набор правил для тестов: число правил считается из файла, а не пишется руками."""

from __future__ import annotations

from pathlib import Path

from svetofor.rules.loader import load_ruleset_file

RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
STARTER_RULES = load_ruleset_file(RULES_FILE).rules
RULE_COUNT = len(STARTER_RULES)
# в пакете по 44-ФЗ правила только для 223-ФЗ не применяются и в отчёт не попадают
RULE_COUNT_44 = sum(1 for rule in STARTER_RULES if "44-ФЗ" in rule.law)
