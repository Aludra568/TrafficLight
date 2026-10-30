"""Вход, роли и формат ошибок через HTTP на настоящей БД."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from svetofor.api.deps import Methodologist
from svetofor.seed.users import seed_users

pytestmark = pytest.mark.pg


@pytest.fixture
def seeded(db: Session) -> Session:
    assert seed_users(db, "demo") == 3
    return db


def login(client: Any, email: str, password: str = "demo") -> Any:
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_seed_is_idempotent(seeded: Session) -> None:
    assert seed_users(seeded, "demo") == 0


def test_login_and_me(client: Any, seeded: Session) -> None:
    response = login(client, "  Specialist@Demo.ru ")  # регистр и пробелы не мешают
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["role"] == "specialist"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "specialist@demo.ru"


def test_wrong_password_same_message_as_unknown_user(client: Any, seeded: Session) -> None:
    wrong = login(client, "specialist@demo.ru", "nope")
    unknown = login(client, "nobody@demo.ru", "nope")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.json()
        == unknown.json()
        == {"error": {"code": "invalid_credentials", "message": "Неверный email или пароль"}}
    )


def test_me_requires_token(client: Any) -> None:
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Требуется вход в систему"


def test_garbage_token_rejected_in_russian(client: Any) -> None:
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer abc"})
    assert response.status_code == 401
    assert "Войдите снова" in response.json()["error"]["message"]


def test_validation_error_format(client: Any) -> None:
    response = client.post("/api/auth/login", json={"email": "x"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Проверьте заполнение полей"
    assert any(item["field"] == "password" for item in error["details"])


def test_role_hierarchy(client: Any, seeded: Session, db: Session) -> None:
    from svetofor.db.session import get_db

    app = FastAPI()

    @app.get("/only-methodologist")
    def only_methodologist(_: Methodologist) -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_db] = lambda: db
    from svetofor.api.errors import install_error_handlers

    install_error_handlers(app)
    probe = TestClient(app)
    for email, expected in [
        ("specialist@demo.ru", 403),
        ("methodologist@demo.ru", 200),
        ("admin@demo.ru", 200),
    ]:
        token = login(client, email).json()["access_token"]
        status = probe.get(
            "/only-methodologist", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        assert status == expected, email


def test_disabled_user_token_rejected(client: Any, seeded: Session, db: Session) -> None:
    """Уволенный сотрудник с ещё не истёкшим токеном в API не входит — ни токеном, ни паролем."""
    from svetofor.db.models import User

    token = login(client, "specialist@demo.ru").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    user = db.query(User).filter_by(email="specialist@demo.ru").one()
    user.is_active = False
    db.flush()
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert login(client, "specialist@demo.ru").status_code == 401


def test_health_and_metrics(client: Any) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    metrics = client.get("/metrics/")
    assert metrics.status_code == 200
    assert "svetofor_quotes_rejected_total" in metrics.text


def test_settings_hide_secrets(client: Any, seeded: Session) -> None:
    token = login(client, "specialist@demo.ru").json()["access_token"]
    body = client.get("/api/settings", headers={"Authorization": f"Bearer {token}"}).json()
    assert "api_key" not in str(body).lower()
    assert body["limits"]["max_upload_mb"] == 100


def test_unknown_email_still_checks_password(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Аудит безопасности 2026-10-08: без проверки пароля ответ на несуществующую почту был
    быстрее на время Argon2 — по задержке перебирались зарегистрированные адреса."""
    from svetofor.api.routers import auth as auth_router

    calls: list[str] = []
    real = auth_router.verify_password

    def counting(password: str, password_hash: str) -> bool:
        calls.append(password_hash)
        return real(password, password_hash)

    monkeypatch.setattr(auth_router, "verify_password", counting)
    response = api["client"].post(
        "/api/auth/login", json={"email": "nobody@demo.ru", "password": "x"}
    )
    assert response.status_code == 401 and len(calls) == 1
