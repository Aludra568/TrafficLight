"""Повторная постановка не запускает второй конвейер; реальная повторная доставка продолжается."""

import uuid

import pytest

from svetofor.db.enums import CheckStatus
from svetofor.db.models import Check
from svetofor.pipeline.finalize import start_check
from tests.integration.test_api_checks import upload

pytestmark = pytest.mark.pg


def test_queued_only_start_is_atomic_and_preserves_worker_recovery(api) -> None:
    created = upload(api)
    identity = uuid.UUID(created["check_id"])
    with api["factory"]() as db:
        check = db.get(Check, identity)
        check.status, check.started_at = CheckStatus.QUEUED, None
        db.commit()
    assert start_check(api["factory"], identity, queued_only=True)
    assert not start_check(api["factory"], identity, queued_only=True)
    assert start_check(api["factory"], identity)  # настоящий перезапуск прежнего воркера разрешён


def test_only_the_original_task_can_resume_a_running_check(api) -> None:
    created = upload(api)
    identity = uuid.UUID(created["check_id"])
    with api["factory"]() as db:
        check = db.get(Check, identity)
        check.status, check.started_at = CheckStatus.QUEUED, None
        db.commit()
    assert start_check(api["factory"], identity, queued_only=True, task_id="original")
    assert not start_check(api["factory"], identity, queued_only=True, task_id="duplicate")
    assert not start_check(
        api["factory"], identity, queued_only=True, task_id="duplicate", redelivered=True
    )
    assert start_check(
        api["factory"], identity, queued_only=True, task_id="original", redelivered=True
    )
