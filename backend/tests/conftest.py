"""Общие фикстуры тестов.

БД: TEST_DATABASE_URL (CI/Docker) или временный встроенный PostgreSQL
(локально, если установлен pgserver).
Тесты с маркером `pg` пропускаются, если PostgreSQL недоступен.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Свой секрет подписи в тестах: иначе сервис создал бы случайный в томе данных (/data)
os.environ.setdefault("JWT_SECRET", "test-secret-for-pytest-only-0123456789abcdef")

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
LOCAL_CACHE = REPO_DIR / ".cache"  # есть только на машине разработчика


def pytest_configure(config: pytest.Config) -> None:
    # Временные файлы тестов — в репозиторий, а не на системный диск
    if config.option.basetemp is None and LOCAL_CACHE.is_dir():
        # .cache бывает и без tmp (её создаёт scripts/acceptance.py, в том числе в CI), а pytest
        # создаёт только саму basetemp, не родительскую папку
        (LOCAL_CACHE / "tmp").mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(LOCAL_CACHE / "tmp" / "pytest")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """`@pytest.mark.docker("tesseract", "soffice")` — пропуск, если программ нет на машине."""
    for marker in item.iter_markers(name="docker"):
        missing = [name for name in marker.args if shutil.which(name) is None]
        if missing:
            pytest.skip(f"нет {', '.join(missing)} — тест выполняется в Docker-образе")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from tests.support.devdb import start_dev_postgres

    server = start_dev_postgres()
    if server is None:
        pytest.skip("PostgreSQL недоступен: задайте TEST_DATABASE_URL или установите pgserver")
    try:
        yield server.url
    finally:
        server.stop()


@pytest.fixture(scope="session")
def migrated_engine(pg_url: str) -> Iterator[Engine]:
    """Схема БД, созданная настоящими миграциями Alembic (так же, как при запуске приложения)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """Сессия в транзакции, которая откатывается после теста."""
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Настоящие коммиты (как в задачах Celery); после теста все таблицы очищаются."""
    from svetofor.db.models import Base

    yield sessionmaker(bind=migrated_engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def pg_conn(migrated_engine: Engine) -> Iterator[object]:
    with migrated_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        yield conn


@pytest.fixture
def client(db: Session) -> Iterator[object]:
    """HTTP-клиент API, работающий в транзакции теста (всё откатывается после теста)."""
    from fastapi.testclient import TestClient

    from svetofor.api.main import create_app
    from svetofor.db.session import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
