"""Celery в режиме «сразу в процессе» с подменёнными зависимостями — для сквозных тестов."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from svetofor.core.config import LLMMode, get_settings
from svetofor.llm.backend import ChatBackend
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.pipeline.ingest import IngestConfig
from svetofor.pipeline.orchestrator import AnalysisDeps
from svetofor.rules.verify import QuoteVerifier
from svetofor.tasks import checks
from svetofor.tasks.celery_app import celery_app
from tests.support.fake_models import StemEmbedder


@contextmanager
def eager_celery(
    session_factory: Callable[[], Session],
    packages_dir: Path,
    backend: ChatBackend | None,
    embedder: StemEmbedder | None = None,
) -> Iterator[checks.Services]:
    keys = ("task_always_eager", "broker_url", "result_backend", "task_store_eager_result")
    saved = {k: celery_app.conf[k] for k in keys}
    celery_app.conf.update(
        task_always_eager=True,
        broker_url="memory://",
        result_backend="cache+memory://",
        task_store_eager_result=True,
    )
    gateway = (
        LLMGateway(
            mode=LLMMode.LIVE, model_main="m", model_fast="m", backend=backend, cache=MemoryCache()
        )
        if backend is not None
        else None
    )
    services = checks.Services(
        settings=get_settings(),
        session_factory=session_factory,
        ingest=IngestConfig(packages_dir=packages_dir),
        analysis=AnalysisDeps(QuoteVerifier(), gateway, embedder or StemEmbedder()),
    )
    checks.configure(services)
    try:
        yield services
    finally:
        checks.configure(None)
        celery_app.conf.update(saved)
