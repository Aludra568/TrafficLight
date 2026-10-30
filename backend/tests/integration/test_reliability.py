"""Надёжность конвейера: зацикленный документ, зависшая проверка, сторож при чтении через API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import CheckStatus, DocType, DocumentStatus
from svetofor.db.models import Check, Document
from svetofor.pipeline.finalize import STALLED_MESSAGE, fail_stalled
from svetofor.pipeline.ingest import CRASHED_MESSAGE, register_attempt
from svetofor.pipeline.progress import StageTracker, init_stages
from tests.integration.conftest import auth
from tests.support.factories import make_document, make_package

pytestmark = pytest.mark.pg
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _check(db: Session, status: CheckStatus, heartbeat: datetime | None) -> Check:
    package = make_package(db, f"Пакет {status.value} {heartbeat}")
    check = Check(
        package_id=package.id, status=status, heartbeat_at=heartbeat, started_at=heartbeat
    )
    db.add(check)
    db.flush()
    return check


def test_document_that_kills_worker_is_not_retried_forever(
    session_factory: sessionmaker[Session],
) -> None:
    """Воркер дважды умер посреди документа — третья доставка помечает его «не удалось»."""
    with session_factory() as db:
        document = make_document(db, make_package(db), DocType.CONTRACT)
        document.status = DocumentStatus.PENDING
        db.commit()
        document_id = document.id
    assert register_attempt(session_factory, document_id)  # 1-я доставка
    assert register_attempt(session_factory, document_id)  # воркер умер — 2-я
    assert not register_attempt(session_factory, document_id)  # снова умер — хватит
    with session_factory() as db:
        stored = db.get(Document, document_id)
        assert stored is not None and stored.status is DocumentStatus.FAILED
        assert stored.error == CRASHED_MESSAGE and stored.attempts == 0


def test_ready_document_attempt_is_free(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        document = make_document(db, make_package(db), DocType.CONTRACT)  # READY
        db.commit()
        document_id = document.id
    for _ in range(5):
        assert register_attempt(session_factory, document_id)
    with session_factory() as db:
        stored = db.get(Document, document_id)
        assert stored is not None and stored.attempts == 0


def test_watchdog_fails_only_stalled_active_checks(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        stalled = _check(db, CheckStatus.RUNNING, NOW - timedelta(minutes=90))
        alive = _check(db, CheckStatus.RUNNING, NOW - timedelta(minutes=40))
        queued_long = _check(db, CheckStatus.QUEUED, NOW - timedelta(hours=2))
        done = _check(db, CheckStatus.DONE, NOW - timedelta(days=1))
        db.commit()
        ids = {c.id: c for c in (stalled, alive, queued_long, done)}
    assert set(fail_stalled(session_factory, now=NOW)) == {stalled.id, queued_long.id}
    with session_factory() as db:
        statuses = {i: db.get(Check, i) for i in ids}
        assert statuses[stalled.id].status is CheckStatus.FAILED  # type: ignore[union-attr]
        assert statuses[stalled.id].error == STALLED_MESSAGE  # type: ignore[union-attr]
        assert statuses[alive.id].status is CheckStatus.RUNNING  # type: ignore[union-attr]
        assert statuses[done.id].status is CheckStatus.DONE  # type: ignore[union-attr]


def test_stage_progress_is_heartbeat(session_factory: sessionmaker[Session]) -> None:
    from svetofor.db.enums import Stage

    with session_factory() as db:
        check = _check(db, CheckStatus.RUNNING, NOW - timedelta(minutes=90))
        init_stages(db, check.id)
        db.commit()
        check_id = check.id
    StageTracker(session_factory, check_id).progress(Stage.CONVERT, 50, "Документ 1 из 2")
    assert fail_stalled(session_factory, [check_id]) == []  # пульс свежий


def test_api_shows_stalled_check_as_failed(
    api: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        check = _check(db, CheckStatus.RUNNING, datetime.now(UTC) - timedelta(minutes=75))
        db.commit()
        check_id = check.id
    # пакет без владельца видят только методолог и администратор (специалисту — 404)
    headers = auth(api, "methodologist")
    body = api["client"].get(f"/api/checks/{check_id}", headers=headers).json()
    assert body["status"] == "failed" and body["error"] == STALLED_MESSAGE


def test_unpack_timeout_is_clear_failure(
    session_factory: sessionmaker[Session], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лимит задачи истёк посреди распаковки: понятная причина, а не «внутренняя ошибка»."""
    from celery.exceptions import SoftTimeLimitExceeded

    from svetofor.db.enums import Stage, StageStatus
    from svetofor.db.models import CheckStage
    from svetofor.tasks import checks
    from tests.support.eager import eager_celery

    def slow(*_: object, **__: object) -> list[object]:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(checks, "prepare_package", slow)
    with session_factory() as db:
        check = _check(db, CheckStatus.QUEUED, None)
        init_stages(db, check.id)
        db.commit()
        check_id = check.id
    with eager_celery(session_factory, tmp_path, None):
        checks.run_check(str(check_id))
    with session_factory() as db:
        stored = db.get(Check, check_id)
        assert stored is not None and stored.status is CheckStatus.FAILED
        assert stored.error == checks.UNPACK_TOO_LONG
        stage = db.query(CheckStage).filter_by(check_id=check_id, stage=Stage.UNPACK.value).one()
        assert stage.status == StageStatus.FAILED
    assert checks.PREPARE_TIME_LIMIT > checks.ARCHIVE_TIMEOUT


def test_check_closed_by_watchdog_while_queued_is_not_started(
    session_factory: sessionmaker[Session], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сторож закрыл проверку, пока задача ждала в очереди: задача её не оживляет (иначе
    второй конвейер по тем же документам, если пользователь уже нажал «перепроверить»)."""
    from svetofor.tasks import checks
    from tests.support.eager import eager_celery

    def must_not_run(*_: object, **__: object) -> list[object]:
        raise AssertionError("распаковка закрытой проверки")

    monkeypatch.setattr(checks, "prepare_package", must_not_run)
    with session_factory() as db:
        check = _check(db, CheckStatus.QUEUED, None)
        check.created_at = NOW - timedelta(hours=3)
        db.commit()
        check_id = check.id
    assert fail_stalled(session_factory, [check_id]) == [check_id]
    with eager_celery(session_factory, tmp_path, None):
        checks.run_check(str(check_id))
        checks.run_check(str(uuid.uuid4()))  # удалённая проверка — тоже тихо
    with session_factory() as db:
        stored = db.get(Check, check_id)
        assert stored is not None and stored.status is CheckStatus.FAILED
        assert stored.error == STALLED_MESSAGE
