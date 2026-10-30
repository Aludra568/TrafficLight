"""Структурированные логи (structlog): JSON в контейнерах, читаемый вывод в консоли разработчика.

Правило проекта: в логи не пишутся тексты документов и ключи — только идентификаторы и метрики.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    log_level = logging.getLevelName(level.upper())
    if not isinstance(log_level, int):
        log_level = logging.INFO

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Сторонние библиотеки (uvicorn, celery, sqlalchemy) пишут через стандартный logging.
    logging.basicConfig(
        level=log_level, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s"
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
