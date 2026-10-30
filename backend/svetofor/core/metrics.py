"""Метрики Prometheus. У заказчика Prometheus/Zabbix — метрики сразу подключаются к их мониторингу.

API отдаёт /metrics. Воркеры Celery — отдельные процессы в своих контейнерах: каждый поднимает
собственный HTTP-эндпоинт метрик (порт METRICS_PORT, см. tasks/celery_app.py).
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

STAGE_SECONDS = Histogram(
    "svetofor_stage_seconds",
    "Длительность этапов проверки",
    ["stage"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180, 300),
)
CHECKS_TOTAL = Counter(
    "svetofor_checks_total", "Завершённые проверки по итоговому статусу", ["status"]
)
DOCUMENTS_TOTAL = Counter(
    "svetofor_documents_total", "Обработанные документы по статусу", ["status"]
)
LLM_LATENCY = Histogram(
    "svetofor_llm_latency_seconds",
    "Время ответа LLM",
    ["task", "model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
LLM_TOKENS = Counter("svetofor_llm_tokens_total", "Токены LLM", ["direction", "model"])
LLM_CACHE_HITS = Counter("svetofor_llm_cache_hits_total", "Ответы LLM, взятые из кэша", ["task"])
LLM_ERRORS = Counter("svetofor_llm_errors_total", "Ошибки вызовов LLM", ["task", "kind"])
EXPLANATIONS_DROPPED = Counter(
    "svetofor_llm_explanations_dropped_total",
    "Пояснения модели, не показанные: в них числа, которых нет в цитате и правиле",
)
QUOTES_REJECTED = Counter(
    "svetofor_quotes_rejected_total",
    "Замечания, отброшенные проверкой цитат (цитата не найдена в документе)",
)
