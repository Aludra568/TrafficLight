"""Локальный демо-запуск разрешён, стандартный пароль на внешнем адресе — нет."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from svetofor.core.config import Settings

PUBLIC_BIND = "0.0.0.0"  # noqa: S104 — вход теста, сетевой сокет не создаётся


@pytest.mark.parametrize(
    "exposure",
    [{"app_host": PUBLIC_BIND}, {"public_app_url": "https://svetofor.example.org"}],
)
def test_public_exposure_rejects_default_demo_password(exposure):
    with pytest.raises(ValidationError, match="DEMO_USERS"):
        Settings(_env_file=None, demo_users=True, demo_users_password="demo", **exposure)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"demo_users": False},
        {"demo_users_password": "local-test-password-42"},
        {
            "app_host": "127.0.0.1",
            "public_app_url": "http://[",
            "demo_users_password": "local-test-password-42",
        },
    ],
)
def test_local_demo_and_safe_public_configuration_remain_available(overrides):
    exposed = {"app_host": PUBLIC_BIND} if overrides else {}
    settings = Settings(_env_file=None, **(exposed | overrides))
    assert settings.demo_users is overrides.get("demo_users", True)


def test_compose_binds_local_demo_to_loopback_by_default():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    )
    assert compose["services"]["nginx"]["ports"] == ["${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:80"]
    assert compose["x-app"]["environment"]["APP_HOST"] == "${APP_HOST:-127.0.0.1}"
