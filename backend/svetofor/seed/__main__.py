"""Сиды при старте API: `python -m svetofor.seed`. Идемпотентно."""

from __future__ import annotations

from svetofor.core.config import get_settings
from svetofor.core.logging import configure_logging, get_logger
from svetofor.db.session import session_scope
from svetofor.seed.rules import seed_rules
from svetofor.seed.users import sync_demo_users


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("seed")
    with session_scope() as db:
        log.info(
            "seed_users",
            **sync_demo_users(db, settings.demo_users, settings.demo_users_password),
        )
        if settings.rules_dir.is_dir():
            log.info("seed_rules", loaded=seed_rules(db, settings.rules_dir))
        else:
            log.warning("seed_rules_dir_missing", path=str(settings.rules_dir))


if __name__ == "__main__":
    main()
