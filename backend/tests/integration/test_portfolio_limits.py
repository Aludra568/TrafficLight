"""Негативные сценарии ревью: цепочки задач, квоты и суммарный размер ЕИС."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from svetofor.core.config import get_settings
from svetofor.db.portfolio import Portfolio
from svetofor.portfolios.dispatch import next_token
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_portfolios import submit

pytestmark = pytest.mark.pg


def test_repeated_http_and_task_delivery_keep_one_dispatch_chain(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []
    monkeypatch.setattr(
        "svetofor.tasks.portfolios.dispatch_portfolio.delay", lambda *args: sent.append(args)
    )
    key = str(uuid.uuid4())
    first = submit(api, [("Пакет.zip", b"same")], key)
    identity = uuid.UUID(first.json()["id"])
    for _ in range(4):
        assert submit(api, [("Пакет.zip", b"same")], key).status_code == 202
        assert (
            api["client"].post(f"/api/portfolios/{identity}/retry", headers=auth(api)).status_code
            == 202
        )
    assert len(sent) == 1
    token = sent[0][1]
    assert next_token(api["factory"], identity, token)
    assert next_token(api["factory"], identity, token) is None


def test_pending_quota_and_delete_release_staged_input(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    monkeypatch.setattr(get_settings(), "max_pending_portfolios", 1)
    first = submit(api, [("Одна.zip", b"first")])
    identity = uuid.UUID(first.json()["id"])
    assert submit(api, [("Вторая.zip", b"second")]).status_code == 429
    with api["factory"]() as db:
        source = Path(db.get(Portfolio, identity).items[0].source_path)
    assert source.is_file()
    assert api["client"].delete(f"/api/portfolios/{identity}", headers=auth(api)).status_code == 204
    assert not source.exists()
    assert submit(api, [("Вторая.zip", b"second")]).status_code == 202


def test_staged_storage_budget_rejects_without_leaving_new_input(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    monkeypatch.setattr(get_settings(), "max_portfolio_storage_mb", 1)
    assert submit(api, [("Одна.zip", b"x" * 700000)]).status_code == 202
    assert submit(api, [("Другая.zip", b"x" * 700000)]).status_code == 429
    files = [p for p in (get_settings().data_dir / "portfolio_uploads").rglob("*") if p.is_file()]
    assert len(files) == 1


def test_eis_budget_counts_downloaded_bytes_before_compression(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zipfile

    archive = build_upload(api["tmp"])
    with zipfile.ZipFile(archive) as source:
        name = source.namelist()[0]
        document = source.read(name) + b"\0" * 700000
    monkeypatch.setattr(get_settings(), "max_portfolio_mb", 1)
    monkeypatch.setattr(
        "svetofor.portfolios.processing.fetch_package",
        lambda *_, **__: SimpleNamespace(files=[(name, document)]),
    )
    response = api["client"].post(
        "/api/portfolios",
        headers=auth(api),
        data={
            "title": "Размер ЕИС",
            "request_key": str(uuid.uuid4()),
            "numbers": "0373100000126000001\n0373100000126000002",
        },
    )
    assert response.status_code == 202
    result = api["client"].get(f"/api/portfolios/{response.json()['id']}", headers=auth(api)).json()
    assert "Превышен общий размер" in result["items"][1]["error"]


def test_retry_cannot_bypass_pending_quota(api, monkeypatch) -> None:
    from svetofor.pipeline.eis import EisError

    def unavailable(*_, **__):
        raise EisError("ЕИС временно недоступна")

    monkeypatch.setattr("svetofor.portfolios.processing.fetch_package", unavailable)
    identities = []
    for _ in range(2):
        response = api["client"].post(
            "/api/portfolios",
            headers=auth(api),
            data={
                "title": "ЕИС",
                "request_key": str(uuid.uuid4()),
                "numbers": "0373100000126000001",
            },
        )
        assert response.status_code == 202
        identities.append(response.json()["id"])
    monkeypatch.setattr(get_settings(), "max_pending_portfolios", 1)
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    first = api["client"].post(f"/api/portfolios/{identities[0]}/retry", headers=auth(api))
    second = api["client"].post(f"/api/portfolios/{identities[1]}/retry", headers=auth(api))
    assert (first.status_code, second.status_code) == (202, 429)


def test_crash_after_token_rotation_recovers_once(api, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from svetofor.tasks.celery_app import celery_app
    from svetofor.tasks.portfolio_recovery import recover_dispatch
    from svetofor.tasks.portfolios import dispatch_portfolio

    sent = []
    monkeypatch.setattr(dispatch_portfolio, "delay", lambda *args: sent.append(args))
    created = submit(api, [("Пакет.zip", b"data")])
    identity = uuid.UUID(created.json()["id"])
    old_token = sent[0][1]
    successor = next_token(api["factory"], identity, old_token)
    recovery = []
    monkeypatch.setattr(recover_dispatch, "apply_async", lambda **kwargs: recovery.append(kwargs))
    with monkeypatch.context() as patch:
        patch.setattr(celery_app.conf, "task_always_eager", False)
        dispatch_portfolio.run(str(identity), old_token)
    assert recovery[0]["args"] == [str(identity), successor]
    with api["factory"]() as db:
        portfolio = db.get(Portfolio, identity)
        portfolio.dispatch_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    resumed = []
    monkeypatch.setattr(
        "svetofor.tasks.portfolio_recovery.enqueue", lambda *args: resumed.append(args)
    )
    recover_dispatch.run(str(identity), successor)
    recover_dispatch.run(str(identity), successor)
    assert len(resumed) == 1
