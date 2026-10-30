"""Локальный запуск API для разработки интерфейса — без Docker, Redis и воркеров.

PostgreSQL — встроенный (pgserver, как в тестах) и постоянный (переживает перезапуск), задачи
Celery выполняются прямо в процессе API, эмбеддингов нет (поиск по тексту), модели нет
(демо-режим). Файлы — в .cache/devdata, база — в SVETOFOR_DEV_DIR/devdb.

    cd backend
    .venv\\Scripts\\python scripts\\dev_server.py        # http://127.0.0.1:8000/api/docs
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

if TYPE_CHECKING:
    from tests.support.devdb import DevPostgres

# база и бинарники PostgreSQL — на D:, если он есть (системный диск бережём), иначе в профиле
if Path("D:/").exists():
    os.environ.setdefault("SVETOFOR_DEV_DIR", r"D:\svetofor-dev")


def load_root_env() -> None:
    """`.env` в корне проекта (ключ модели, GREEN_SWEEP): сервер запускается из backend/, а
    настройки сами читают `.env` только из текущей папки. Уже заданное окружение важнее."""
    from dotenv import dotenv_values

    for key, value in dotenv_values(REPO / ".env").items():
        if value is not None:
            os.environ.setdefault(key, value)


def prepare_environment() -> DevPostgres:
    """База, миграции, сиды и Celery «в процессе» — общее для сервера и `dev_eval.py`."""
    load_root_env()  # инфраструктуру (база, Redis, эмбеддинги) ниже всё равно задаёт этот сервер
    from tests.support.devdb import start_persistent_postgres

    # база постоянная (SVETOFOR_DEV_DIR\devdb): проверки и вход переживают перезапуск сервера
    server = start_persistent_postgres()
    if server is None:
        raise SystemExit("Нет встроенного PostgreSQL (pgserver)")
    data = REPO / ".cache" / "devdata"
    data.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "DATABASE_URL": server.url,
            "DATA_DIR": str(data),
            "RULES_DIR": str(REPO / "rules"),
            # наборы оценки и результаты (по умолчанию /app/eval — путь в образе)
            "EVAL_DIR": str(REPO / "eval"),
            "DEMO_PACKAGE": str(REPO / "samples" / "demo_44fz_postavka.zip"),
            "DEMO_CASSETTE": str(REPO / "samples" / "demo_llm_cassette.jsonl"),
            "CELERY_EAGER": "true",
            "EMBED_URL": "",
            "REDIS_URL": "redis://127.0.0.1:6399/0",  # нет Redis — кэш и метрики его пропускают
            "LOG_JSON": "false",
        }
    )
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", server.url)
    command.upgrade(cfg, "head")

    from svetofor.seed.__main__ import main as seed

    seed()

    from svetofor.tasks.celery_app import celery_app

    celery_app.conf.update(
        task_always_eager=True,
        broker_url="memory://",
        result_backend="cache+memory://",
        task_store_eager_result=True,
    )
    return server


def main() -> None:
    server = prepare_environment()
    import uvicorn

    from svetofor.api.main import create_app

    try:
        uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_level="warning")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
