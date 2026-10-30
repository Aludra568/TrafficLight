"""Общие фикстуры интеграционных тестов API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from svetofor.api.deps import get_session_factory
from svetofor.core.config import get_settings
from svetofor.db.session import get_db
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.seed.users import seed_users
from tests.integration.test_analysis import FACTS, RULES_FILE, VIOLATIONS
from tests.support.eager import eager_celery
from tests.support.fake_models import RoutingBackend


def auth(api: dict[str, Any], role: str = "specialist") -> dict[str, str]:
    return {"Authorization": f"Bearer {api['tokens'][role]}"}


@pytest.fixture
def api(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    from svetofor.api.main import create_app

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    with session_factory() as db:
        seed_users(db, settings.demo_users_password)
        import_ruleset(db, load_ruleset_file(RULES_FILE))
        db.commit()
    app = create_app()

    def db_dependency() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    backend = RoutingBackend(dict(FACTS), dict(VIOLATIONS))
    with eager_celery(session_factory, settings.packages_dir, backend), TestClient(app) as client:
        tokens = {
            role: client.post(
                "/api/auth/login",
                json={"email": f"{role}@demo.ru", "password": settings.demo_users_password},
            ).json()["access_token"]
            for role in ("specialist", "methodologist", "admin")
        }
        yield {
            "client": client,
            "tokens": tokens,
            "factory": session_factory,
            "tmp": tmp_path,
            "backend": backend,
        }
