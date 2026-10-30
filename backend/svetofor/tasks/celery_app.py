"""Celery-приложение.

Очереди: `ingest` — конвертация, OCR, разбор (CPU, prefork); `llm` — вызовы модели (сеть,
threads); `default` — оркестрация и итог. Задачи подтверждаются после выполнения
(`acks_late`): воркер упал — задача достанется другому. Каждый воркер отдаёт метрики
Prometheus на своём порту (`METRICS_PORT`).
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from celery.signals import worker_init

from svetofor.core.config import Settings, get_settings

QUEUE_INGEST, QUEUE_LLM, QUEUE_DEFAULT = "ingest", "llm", "default"


def create_celery(settings: Settings) -> Celery:
    # задачи подключаются лениво (include), иначе цикл импорта: задачи импортируют приложение
    app = Celery(
        "svetofor",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=[
            "svetofor.tasks.checks",
            "svetofor.tasks.rules",
            "svetofor.tasks.exports",
            "svetofor.tasks.eis",
        ],
    )
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_default_queue=QUEUE_DEFAULT,
        task_routes={
            "checks.process_document": {"queue": QUEUE_INGEST},
            "checks.embed_chunks": {"queue": QUEUE_INGEST},
            # загрузка из ЕИС — воркер с выходом в сеть, не API
            "eis.fetch": {"queue": QUEUE_INGEST},
            "checks.extract_facts": {"queue": QUEUE_LLM},
            # группы правил с моделью — параллельно в пуле потоков очереди llm, иначе
            # 8 групп шли бы друг за другом и 20 страниц не уложились бы в 2 минуты
            "checks.evaluate_rules": {"queue": QUEUE_LLM},
            "rules.sandbox": {"queue": QUEUE_LLM},
        },
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        task_always_eager=settings.celery_eager,
        task_eager_propagates=True,
        timezone="Europe/Moscow",
    )
    return app


celery_app = create_celery(get_settings())


@worker_init.connect
def _start_metrics_server(**_: Any) -> None:
    """Метрики воркера. Для prefork нужен общий каталог PROMETHEUS_MULTIPROC_DIR."""
    port = get_settings().metrics_port
    if not port:
        return
    from prometheus_client import CollectorRegistry, start_http_server
    from prometheus_client.multiprocess import MultiProcessCollector

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        MultiProcessCollector(registry)
        start_http_server(port, registry=registry)
    else:
        start_http_server(port)
