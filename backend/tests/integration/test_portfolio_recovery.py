"""Восстановление после сбоев, конкурентность и совместимость миграции."""

from __future__ import annotations

import io
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from svetofor.db.models import Check, Finding
from svetofor.pipeline.eis import EisError
from svetofor.portfolios.register import register
from svetofor.portfolios.uploads import Source
from tests.conftest import BACKEND_DIR
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_api_checks import upload
from tests.integration.test_portfolios import submit

pytestmark = pytest.mark.pg


def test_retry_queue_failure_does_not_rerun_successful_checks(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from svetofor.tasks.checks import run_check

    content = build_upload(api["tmp"]).read_bytes()
    original = run_check.delay
    calls = 0

    def enqueue(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("test broker failure")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(run_check, "delay", enqueue)
        created = submit(api, [("Первая.zip", content), ("Вторая.zip", content)])
    assert created.status_code == 202
    base = f"/api/portfolios/{created.json()['id']}"
    before = api["client"].get(base, headers=auth(api)).json()
    assert (before["done"], before["failed"]) == (1, 1)
    successful = next(item["check_id"] for item in before["items"] if item["status"] == "done")
    assert api["client"].post(base + "/retry", headers=auth(api)).status_code == 202
    after = api["client"].get(base, headers=auth(api)).json()
    assert (after["done"], after["failed"]) == (2, 0)
    assert successful in [item["check_id"] for item in after["items"]]


def test_eis_duplicates_and_partial_fetch_error(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = build_upload(api["tmp"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        files = [(name, archive.read(name)) for name in archive.namelist()]

    def fetch(number: str, **_):
        if number.endswith("2"):
            raise EisError("Закупка временно недоступна")
        return SimpleNamespace(files=files)

    monkeypatch.setattr("svetofor.portfolios.processing.fetch_package", fetch)
    first, second = "0373100000126000001", "0373100000126000002"
    response = api["client"].post(
        "/api/portfolios",
        headers=auth(api),
        data={
            "title": "ЕИС",
            "request_key": str(uuid.uuid4()),
            "numbers": f"{first}\n{second}\n{first}",
        },
    )
    assert response.status_code == 202, response.text
    result = api["client"].get(f"/api/portfolios/{response.json()['id']}", headers=auth(api)).json()
    assert (result["total"], result["done"], result["failed"]) == (2, 1, 1)
    assert "недоступна" in result["items"][1]["error"]


def test_simultaneous_registration_uses_one_portfolio(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    response = submit(api, [("Одна.zip", b"data")])
    with api["factory"]() as db:
        from svetofor.db.portfolio import Portfolio

        portfolio = db.get(Portfolio, uuid.UUID(response.json()["id"]))
        owner = portfolio.owner_id
    key = str(uuid.uuid4())

    def create():
        with api["factory"]() as db:
            result, _ = register(
                db, owner, "Конкурентный запуск", key, [Source("eis", "0373100000126000001")]
            )
            db.commit()
            return result.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results[0] == results[1]


def test_migration_down_and_up_preserves_existing_findings(
    api: dict[str, Any], pg_url: str
) -> None:
    created = upload(api)
    identity = uuid.UUID(created["check_id"])
    with api["factory"]() as db:
        before = [
            (f.id, f.quote) for f in db.scalars(select(Finding).where(Finding.check_id == identity))
        ]
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", pg_url)
    try:
        command.downgrade(config, "0007")
    finally:
        command.upgrade(config, "head")
    with api["factory"]() as db:
        assert db.get(Check, identity) is not None
        after = list(db.scalars(select(Finding).where(Finding.check_id == identity)))
        assert sorted((f.id, f.quote) for f in after) == sorted(before)
        assert all(f.handling_version == 0 and f.handling_status.value == "new" for f in after)
