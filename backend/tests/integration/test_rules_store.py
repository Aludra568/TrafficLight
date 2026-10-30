"""Правила в БД: идемпотентный импорт, новые версии только при изменениях, воспроизводимость."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from svetofor.db.models import Rule as RuleRow
from svetofor.db.models import RuleVersion
from svetofor.rules.loader import dump_ruleset, load_ruleset_file, load_ruleset_text
from svetofor.rules.store import (
    export_ruleset,
    import_ruleset,
    latest_ruleset_version,
    ruleset_rules,
)

pytestmark = pytest.mark.pg
RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_import_is_idempotent(db: Session) -> None:
    ruleset = load_ruleset_file(RULES_FILE)
    first = import_ruleset(db, ruleset)
    versions_after_first = count(db, RuleVersion)
    second = import_ruleset(db, ruleset)
    assert first.id == second.id and first.version == 1
    assert count(db, RuleVersion) == versions_after_first == len(ruleset.rules)
    assert count(db, RuleRow) == len(ruleset.rules)


def test_changed_rule_gets_new_version_and_old_check_stays_reproducible(db: Session) -> None:
    ruleset = load_ruleset_file(RULES_FILE)
    v1 = import_ruleset(db, ruleset)
    changed = ruleset.model_copy(deep=True)
    pay = next(r for r in changed.rules if r.id == "PAY-01")
    pay.title = "Срок оплаты дольше семи рабочих дней"
    v2 = import_ruleset(db, changed, note="уточнили название")
    assert v2.version == 2 and v2.id != v1.id
    assert count(db, RuleVersion) == len(ruleset.rules) + 1  # новая версия только у PAY-01
    old = dict((r.id, r) for _, r in ruleset_rules(db, v1))
    new = dict((r.id, r) for _, r in ruleset_rules(db, v2))
    assert (
        old["PAY-01"].title == "Срок оплаты дольше 7 рабочих дней"
    )  # старая проверка — старый текст
    assert new["PAY-01"].title == "Срок оплаты дольше семи рабочих дней"
    row = db.scalar(select(RuleRow).where(RuleRow.code == "PAY-01"))
    assert row is not None and row.current_version is not None
    assert (
        row.current_version.version == 2 and row.current_version.change_note == "уточнили название"
    )
    assert latest_ruleset_version(db, ruleset.ruleset.code).id == v2.id  # type: ignore[union-attr]


def test_rules_restored_in_order_and_equal(db: Session) -> None:
    ruleset = load_ruleset_file(RULES_FILE)
    version = import_ruleset(db, ruleset)
    restored = [rule for _, rule in ruleset_rules(db, version)]
    assert restored == ruleset.rules


def test_export_roundtrip(db: Session) -> None:
    ruleset = load_ruleset_file(RULES_FILE)
    import_ruleset(db, ruleset)
    exported = export_ruleset(db, ruleset.ruleset.code)
    assert exported is not None
    again = load_ruleset_text(dump_ruleset(exported))
    assert again.rules == ruleset.rules
    assert export_ruleset(db, "no-such-ruleset") is None
