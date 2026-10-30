"""Загрузить набор правил из YAML в БД: `python -m svetofor.seed.import_rules rules/набор.yaml`.

В отличие от первой загрузки при старте, обновляет существующий набор: изменённые правила
получают новые версии, набор — новую версию. Старые проверки остаются на своих версиях.
"""

from __future__ import annotations

import sys
from pathlib import Path

from svetofor.core.config import get_settings
from svetofor.core.logging import configure_logging
from svetofor.db.session import session_scope
from svetofor.rules.loader import RulesetError, load_ruleset_file
from svetofor.rules.store import import_ruleset


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if len(args) != 1:
        print("Использование: python -m svetofor.seed.import_rules путь/к/набору.yaml")
        return 2
    try:
        ruleset = load_ruleset_file(Path(args[0]))
    except (OSError, RulesetError) as exc:
        print(exc)
        return 1
    with session_scope() as db:
        version = import_ruleset(db, ruleset, note=f"импорт {Path(args[0]).name}")
        print(
            f"Набор «{ruleset.ruleset.code}»: версия {version.version}, правил {len(ruleset.rules)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
