"""Задачи для правил: песочница (модель отвечает секунды — HTTP-обработчик её не ждёт)."""

from __future__ import annotations

from typing import Any

from svetofor.core.logging import get_logger
from svetofor.db.enums import DocType
from svetofor.pipeline.sandbox import run_sandbox
from svetofor.rules.loader import parse_rule
from svetofor.tasks.celery_app import celery_app
from svetofor.tasks.checks import services

log = get_logger(__name__)


@celery_app.task(name="rules.sandbox", soft_time_limit=180)
def sandbox_task(rule_body: dict[str, Any], text: str, doc_type: str | None) -> dict[str, Any]:
    rule = parse_rule(rule_body)
    return run_sandbox(rule, text, DocType(doc_type) if doc_type else None, services().analysis)
