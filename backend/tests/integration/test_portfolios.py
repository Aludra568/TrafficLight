"""Сквозные критерии портфеля: независимость закупок, повторы, очередь и доступ."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from svetofor.core.config import get_settings
from svetofor.db.enums import CheckStatus
from svetofor.db.models import Check
from svetofor.db.portfolio import PortfolioItem
from svetofor.portfolios.queue import claim_items
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_api_access import second_specialist

pytestmark = pytest.mark.pg


def submit(api: dict[str, Any], files: list[tuple[str, bytes]], key: str | None = None):
    return api["client"].post(
        "/api/portfolios",
        headers=auth(api),
        data={"title": "Поставки ноября", "request_key": key or str(uuid.uuid4())},
        files=[("files", (name, content, "application/zip")) for name, content in files],
    )


def test_three_procurements_one_broken_and_deep_links(api: dict[str, Any]) -> None:
    content = build_upload(api["tmp"]).read_bytes()
    created = submit(
        api, [("Первая.zip", content), ("Сломана.zip", b"bad zip"), ("Третья.zip", content)]
    )
    assert created.status_code == 202, created.text
    url = f"/api/portfolios/{created.json()['id']}"
    result = api["client"].get(url, headers=auth(api))
    assert result.status_code == 200, result.text
    data = result.json()
    assert (data["total"], data["done"], data["failed"]) == (3, 2, 1)
    assert data["complete"] and data["light"] == "red"
    assert data["items"][1]["error"]
    assert len({item["check_id"] for item in data["items"]}) == 3
    assert any(risk["procurements"] == 2 for risk in data["repeated_risks"])
    assert data["findings"][0]["severity"] == "red"
    other = second_specialist(api)
    assert api["client"].get(url, headers=other).status_code == 404
    assert api["client"].get("/api/portfolios", headers=other).json()["total"] == 0
    for suffix in ("/export.xlsx", "/export.docx"):
        assert api["client"].get(url + suffix, headers=other).status_code == 404
        assert api["client"].get(url + suffix, headers=auth(api)).status_code == 200
    assert api["client"].post(url + "/retry", headers=other).status_code == 404


def test_idempotency_rejects_changed_payload_and_preserves_waiting(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    key = str(uuid.uuid4())
    files = [("Одна.zip", b"one")]
    first, repeated = submit(api, files, key), submit(api, files, key)
    assert first.status_code == repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    assert submit(api, [("Одна.zip", b"two")], key).status_code == 409
    data = api["client"].get(f"/api/portfolios/{first.json()['id']}", headers=auth(api)).json()
    assert data["waiting"] == 1 and data["light"] == "gray" and not data["complete"]
    assert api["client"].get("/api/portfolios", headers=auth(api)).json()["total"] == 1


def test_dispatch_respects_user_limit_including_reserved_items(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    monkeypatch.setattr(get_settings(), "max_active_checks_per_user", 1)
    response = submit(api, [(f"{n}.zip", b"file") for n in range(3)])
    identity = uuid.UUID(response.json()["id"])
    with api["factory"]() as db:
        claimed = claim_items(db, identity)
        db.commit()
        assert len(claimed) == 1
    with api["factory"]() as db:
        assert claim_items(db, identity) == []
        item = db.get(PortfolioItem, claimed[0][0])
        item.status = "failed"
        db.commit()
    with api["factory"]() as db:
        assert len(claim_items(db, identity)) == 1
        db.commit()


def test_server_validates_input_before_dispatch(
    api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("svetofor.api.routers.portfolios.enqueue", lambda *_: None)
    monkeypatch.setattr(get_settings(), "max_portfolio_items", 2)
    assert submit(api, [(str(i) + ".zip", b"x") for i in range(2)]).status_code == 202
    assert submit(api, [(str(i) + ".zip", b"x") for i in range(3)]).status_code == 400
    assert submit(api, [("empty.zip", b"")]).status_code == 400
    assert submit(api, [("virus.exe", b"MZ")]).status_code == 415
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    assert submit(api, [("big.zip", b"x" * (1024**2 + 1))]).status_code == 413
    response = api["client"].post(
        "/api/portfolios",
        headers=auth(api),
        data={
            "title": "ЕИС",
            "request_key": str(uuid.uuid4()),
            "numbers": "123\nне номер",
        },
    )
    assert response.status_code == 400


def test_deleted_check_stays_in_denominator(api: dict[str, Any]) -> None:
    content = build_upload(api["tmp"]).read_bytes()
    created = submit(api, [("Закупка.zip", content)]).json()
    with api["factory"]() as db:
        item = db.scalar(
            select(PortfolioItem).where(PortfolioItem.portfolio_id == uuid.UUID(created["id"]))
        )
        check = db.get(Check, item.check_id)
        assert check.status == CheckStatus.DONE
        db.delete(check.package)
        db.commit()
    result = api["client"].get(f"/api/portfolios/{created['id']}", headers=auth(api)).json()
    assert result["total"] == result["failed"] == 1
    assert result["light"] == "gray" and "удалены" in result["items"][0]["error"]
