"""Сборка FastAPI-приложения. Запуск: uvicorn svetofor.api.main:app."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from prometheus_client import make_asgi_app

import svetofor.core.metrics  # noqa: F401 — регистрирует метрики проекта в /metrics
from svetofor import __version__
from svetofor.api.errors import install_error_handlers
from svetofor.api.routers import auth, checks, documents, quality, rules, system
from svetofor.api.security import jwt_secret
from svetofor.core.config import get_settings
from svetofor.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if len(jwt_secret().encode()) < 32:
        log.warning("jwt_secret_short", hint="JWT_SECRET короче 32 байт — ключ слабый")
    if settings.demo_users and settings.demo_users_password == "demo":  # noqa: S105
        # методолог меняет правила — то есть вердикты для всех: на стенде с доступом извне
        # демо-учётки с паролем по умолчанию недопустимы
        log.warning(
            "demo_users_default_password",
            hint="задайте DEMO_USERS_PASSWORD или DEMO_USERS=false вне демонстрации",
        )
    log.info("api_started", version=__version__, llm_mode=settings.effective_llm_mode.value)
    yield


def create_app() -> FastAPI:
    # API_DOCS=false — описание API не отдаётся совсем (404); app.openapi() для файла схемы
    # backend/openapi.json работает и так
    docs = get_settings().api_docs
    app = FastAPI(
        title="Светофор — проверка закупочной документации",
        version=__version__,
        docs_url="/api/docs" if docs else None,
        openapi_url="/api/openapi.json" if docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.middleware("http")(log_request)
    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(checks.router)
    app.include_router(documents.router)
    app.include_router(rules.router)
    app.include_router(quality.router)
    app.mount("/metrics", make_asgi_app())
    return app


async def log_request(request: Request, call_next: Any) -> Response:
    """Журнал запросов без строки параметров: в ней бывает билет событий (SSE).

    uvicorn запускается с --no-access-log — его журнал пишет адрес целиком.
    """
    started = time.perf_counter()
    response: Response = await call_next(request)
    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        ms=round((time.perf_counter() - started) * 1000),
    )
    return response


app = create_app()
