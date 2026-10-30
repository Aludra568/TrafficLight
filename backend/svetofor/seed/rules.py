"""Первая загрузка наборов правил из YAML (папка `rules/`).

Набор загружается, только если его ещё нет в БД: дальше правила живут в БД и меняются
методологом в интерфейсе — перезапуск сервиса не должен откатывать их к файлу.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.db.models import Ruleset
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset


def seed_rules(db: Session, rules_dir: Path) -> list[str]:
    """Загруженные коды наборов (пустой список — всё уже было)."""
    loaded: list[str] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        ruleset = load_ruleset_file(path)
        exists = db.scalar(select(Ruleset.id).where(Ruleset.code == ruleset.ruleset.code))
        if exists is None:
            import_ruleset(db, ruleset, note=f"первая загрузка из {path.name}")
            loaded.append(ruleset.ruleset.code)
    return loaded
