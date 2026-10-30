"""Служебные эндпоинты: живость, готовность, настройки (без секретов)."""

from __future__ import annotations

from typing import Any

import httpx
import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from svetofor import __version__
from svetofor.api.deps import CurrentUser, DbSession
from svetofor.core.config import get_settings
from svetofor.llm.naming import public_model_name

router = APIRouter(prefix="/api", tags=["Служебное"])


@router.get("/health", summary="Сервис жив")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def _check_db(db: DbSession) -> str:
    try:
        db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__}"


def _check_redis(url: str) -> str:
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__}"


def _check_embedder(url: str) -> str:
    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=1.5)
        return "ok" if response.status_code == 200 else f"error: HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return f"error: {type(exc).__name__}"


@router.get("/ready", summary="Готовность зависимостей")
def ready(db: DbSession) -> JSONResponse:
    settings = get_settings()
    checks = {
        "database": _check_db(db),
        "redis": _check_redis(settings.redis_url),
        "embedder": _check_embedder(settings.embed_url),
    }
    ok = all(v == "ok" for v in checks.values())
    body: dict[str, Any] = {
        "status": "ready" if ok else "degraded",
        "checks": checks,
        "llm": {"configured": settings.llm_configured, "mode": settings.effective_llm_mode.value},
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/settings", summary="Текущие настройки (без ключей)")
def public_settings(_: CurrentUser) -> dict[str, Any]:
    s = get_settings()
    return {
        "version": __version__,
        "llm": {
            "configured": s.llm_configured,
            "mode": s.effective_llm_mode.value,
            "model_main": public_model_name(s.llm_model_main),
            "model_fast": public_model_name(s.llm_model_fast_or_main),
            "max_concurrency": s.llm_max_concurrency,
        },
        "embeddings": {"model": s.embed_model, "dim": s.embed_dim},
        "eis_fetch": s.eis_fetch,
        "limits": {
            "max_upload_mb": s.max_upload_mb,
            "max_files": s.max_files,
            "max_pages_per_doc": s.max_pages_per_doc,
        },
    }
