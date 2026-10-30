"""Граф задач Celery в eager-режиме: этапы, итог, честные исходы при сбоях задач."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.core.config import LLMMode, get_settings
from svetofor.db.enums import CheckStatus, RuleOutcome, Stage, StageStatus
from svetofor.db.models import Check, CheckStage, RuleResult
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.pipeline.ingest import IngestConfig, create_check
from svetofor.pipeline.orchestrator import AnalysisDeps
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.rules.verify import QuoteVerifier
from svetofor.tasks import checks
from svetofor.tasks.celery_app import celery_app
from tests.integration.test_analysis import FACTS, RULES_FILE, VIOLATIONS, build_upload
from tests.support.fake_models import RoutingBackend, StemEmbedder
from tests.support.rulesets import RULE_COUNT

pytestmark = pytest.mark.pg


@pytest.fixture
def eager(session_factory: sessionmaker[Session], tmp_path: Path) -> Iterator[checks.Services]:
    saved = {k: celery_app.conf[k] for k in ("task_always_eager", "broker_url", "result_backend")}
    celery_app.conf.update(
        task_always_eager=True, broker_url="memory://", result_backend="cache+memory://"
    )
    gateway = LLMGateway(
        mode=LLMMode.LIVE,
        model_main="m",
        model_fast="m",
        backend=RoutingBackend(FACTS, VIOLATIONS),
        cache=MemoryCache(),
    )
    services = checks.Services(
        settings=get_settings(),
        session_factory=session_factory,
        ingest=IngestConfig(packages_dir=tmp_path / "packages"),
        analysis=AnalysisDeps(QuoteVerifier(), gateway, StemEmbedder()),
    )
    checks.configure(services)
    try:
        yield services
    finally:
        checks.configure(None)
        celery_app.conf.update(saved)


def submit(services: checks.Services, tmp_path: Path) -> Any:
    with services.session_factory() as db:
        version = import_ruleset(db, load_ruleset_file(RULES_FILE))
        check = create_check(
            db,
            upload=build_upload(tmp_path),
            original_name="Закупка.zip",
            config=services.ingest,
            ruleset_version_id=version.id,
        )
        db.commit()
    checks.run_check.delay(str(check.id))
    return check.id


def stages(db: Session, check_id: Any) -> dict[str, CheckStage]:
    return {
        s.stage: s for s in db.scalars(select(CheckStage).where(CheckStage.check_id == check_id))
    }


def test_full_graph(eager: checks.Services, tmp_path: Path) -> None:
    check_id = submit(eager, tmp_path)
    with eager.session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None and check.status is CheckStatus.DONE
        assert check.started_at is not None and check.finished_at is not None
        by_stage = stages(db, check_id)
        assert (
            by_stage[Stage.OCR].status is StageStatus.SKIPPED
            and by_stage[Stage.OCR].detail == "Сканов нет"
        )
        for stage in (
            Stage.UNPACK,
            Stage.CONVERT,
            Stage.STRUCTURE,
            Stage.FACTS,
            Stage.RULES,
            Stage.FINALIZE,
        ):
            assert by_stage[stage].status is StageStatus.DONE, stage
        assert by_stage[Stage.CONVERT].detail == "Готово документов: 2"
        assert set(check.timings) >= {"unpack", "convert", "facts", "rules", "finalize"}
        outcomes = list(db.scalars(select(RuleResult).where(RuleResult.check_id == check_id)))
        assert len(outcomes) == RULE_COUNT


def test_task_failure_gives_honest_outcome(
    eager: checks.Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        if (
            kwargs.get("rule_codes") is None
            and args
            and isinstance(args[-1], list)
            and "LIA-01" in args[-1]
        ):
            raise RuntimeError("сбой воркера")
        return original(*args, **kwargs)

    original = checks.evaluate_rules
    monkeypatch.setattr(checks, "evaluate_rules", broken)
    monkeypatch.setattr(
        checks, "extract_document_facts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    check_id = submit(eager, tmp_path)
    with eager.session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None and check.status is CheckStatus.DONE_WITH_WARNINGS  # не завис
        assert set(check.counters["facts_errors"].values()) == {checks.INTERNAL_ERROR}
        results = {
            r.rule_code: r
            for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
        }
        assert len(results) == RULE_COUNT
        assert results["LIA-01"].outcome is RuleOutcome.NOT_CHECKED
        assert results["LIA-01"].reason == "внутренняя ошибка при проверке правила"


def test_broken_archive_fails_check(eager: checks.Services, tmp_path: Path) -> None:
    upload = tmp_path / "broken.zip"
    upload.write_bytes(b"not a zip")
    with eager.session_factory() as db:
        check = create_check(
            db,
            upload=upload,
            original_name="broken.zip",
            config=eager.ingest,
            ruleset_version_id=None,
        )
        db.commit()
    checks.run_check.delay(str(check.id))
    with eager.session_factory() as db:
        saved = db.get(Check, check.id)
        assert saved is not None and saved.status is CheckStatus.FAILED
        assert saved.error == "Архив повреждён или это не ZIP"
        assert stages(db, check.id)[Stage.UNPACK].status is StageStatus.FAILED
