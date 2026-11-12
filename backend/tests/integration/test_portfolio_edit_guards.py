"""Поздние сбои не меняют новую попытку, подготовка учитывается в квотах."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from svetofor.api.errors import AppError
from svetofor.db.portfolio import Portfolio, PortfolioItem, PortfolioOperation
from svetofor.portfolios.admission import ensure_capacity
from svetofor.tasks.portfolio_edits import fail_operation
from tests.integration.conftest import auth
from tests.integration.test_portfolio_editing import create

pytestmark = pytest.mark.pg


def test_old_failure_cannot_clear_new_attempt(api):
    base, _ = create(api)
    identity = uuid.UUID(base.rsplit("/", 1)[1])
    old = datetime.now(UTC) - timedelta(minutes=11)
    current = datetime.now(UTC)
    with api["factory"]() as db:
        item = db.scalar(select(PortfolioItem).where(PortfolioItem.portfolio_id == identity))
        op = PortfolioOperation(
            portfolio_id=identity,
            request_key=str(uuid.uuid4()),
            request_hash="f" * 64,
            kind="documents",
            status="running",
            started_at=current,
            payload={"item_id": str(item.id), "previous_bytes": 0},
        )
        db.add(op)
        db.flush()
        item.edit_token, item.source_bytes = str(op.id), 123
        operation_id, item_id = op.id, item.id
        db.commit()
    fail_operation(api["factory"], operation_id, "Устаревший сбой", old)
    with api["factory"]() as db:
        assert db.get(PortfolioOperation, operation_id).status == "running"
        item = db.get(PortfolioItem, item_id)
        assert item.edit_token == str(operation_id) and item.source_bytes == 123
    fail_operation(api["factory"], operation_id, "Текущий сбой", current)
    with api["factory"]() as db:
        assert db.get(PortfolioOperation, operation_id).status == "failed"
        assert db.get(PortfolioItem, item_id).edit_token is None


def test_pending_document_edit_counts_toward_quota(api, monkeypatch):
    from svetofor.core.config import get_settings

    base, _ = create(api)
    identity = uuid.UUID(base.rsplit("/", 1)[1])
    monkeypatch.setattr(get_settings(), "max_pending_portfolios", 1)
    with api["factory"]() as db:
        item = db.scalar(select(PortfolioItem).where(PortfolioItem.portfolio_id == identity))
        item.edit_token = str(uuid.uuid4())
        db.commit()
        portfolio = db.get(Portfolio, identity)
        with pytest.raises(AppError) as caught:
            ensure_capacity(db, portfolio.owner_id, 0)
        assert caught.value.status == 429


def test_prepared_revision_exposes_dispatch_failure(api, monkeypatch):
    base, _ = create(api)
    client, headers = api["client"], auth(api)
    item = client.get(base, headers=headers).json()["items"][0]
    monkeypatch.setattr("svetofor.tasks.portfolio_edits.enqueue", lambda *_: "Очередь недоступна")
    response = client.post(
        base + f"/items/{item['id']}/documents",
        headers=headers,
        data={"request_key": str(uuid.uuid4()), "version": "0"},
        files=[("files", ("new.txt", b"additional terms"))],
    )
    assert response.status_code == 202
    current = client.get(base, headers=headers).json()["items"][0]
    assert current["edit_error"] == "Очередь недоступна"
    assert current["check_id"] == item["check_id"]


def test_add_numbers_while_other_item_is_starting(api, monkeypatch):
    base, _ = create(api)
    client, headers = api["client"], auth(api)
    initial = client.get(base, headers=headers).json()["items"][0]
    with api["factory"]() as db:
        db.get(PortfolioItem, uuid.UUID(initial["id"])).status = "starting"
        db.commit()
    monkeypatch.setattr("svetofor.api.routers.portfolio_editing.enqueue", lambda *_: None)
    added = client.post(
        base + "/items",
        headers=headers,
        data={
            "request_key": str(uuid.uuid4()),
            "numbers": "0123456789012345678\n1123456789012345678",
        },
    )
    assert added.status_code == 202, added.text
    data = client.get(base, headers=headers).json()
    assert data["total"] == 3
    assert data["items"][0]["check_id"] == initial["check_id"]
    assert (
        client.delete(base + f"/items/{initial['id']}?version=0", headers=headers).status_code
        == 409
    )
    assert (
        client.get(base + f"/items/{initial['id']}/documents", headers=headers).status_code == 409
    )
    pending = data["items"][1]
    assert (
        client.delete(base + f"/items/{pending['id']}?version=0", headers=headers).status_code
        == 204
    )
    assert client.get(base, headers=headers).json()["total"] == 2


def test_empty_portfolio_exports_explain_empty_state(api):
    import io

    from docx import Document
    from openpyxl import load_workbook

    base, _ = create(api)
    client, headers = api["client"], auth(api)
    item = client.get(base, headers=headers).json()["items"][0]
    assert (
        client.delete(base + f"/items/{item['id']}?version=0", headers=headers).status_code == 204
    )
    word = client.get(base + "/export.docx", headers=headers)
    excel = client.get(base + "/export.xlsx", headers=headers)
    assert word.status_code == excel.status_code == 200
    assert "В портфеле пока нет закупок" in "\n".join(
        p.text for p in Document(io.BytesIO(word.content)).paragraphs
    )
    workbook = load_workbook(io.BytesIO(excel.content))
    assert any(
        cell.value == "В портфеле пока нет закупок"
        for sheet in workbook
        for row in sheet
        for cell in row
    )


def test_replacement_storage_quota_uses_final_size(api, monkeypatch):
    import json

    from svetofor.core.config import get_settings

    base, _ = create(api)
    client, headers = api["client"], auth(api)
    item = client.get(base, headers=headers).json()["items"][0]
    path = base + f"/items/{item['id']}/documents"
    docs = client.get(path, headers=headers).json()["documents"]
    with api["factory"]() as db:
        current = db.get(PortfolioItem, uuid.UUID(item["id"]))
        source = get_settings().data_dir / "portfolio_uploads" / "quota-source.zip"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"old")
        current.source_path, current.source_bytes = str(source), 700 * 1024
        db.commit()
    monkeypatch.setattr(get_settings(), "max_portfolio_storage_mb", 1)
    monkeypatch.setattr("svetofor.api.routers.portfolio_documents.dispatch_edit", lambda *_: None)
    response = client.post(
        path,
        headers=headers,
        data={
            "request_key": str(uuid.uuid4()),
            "version": "0",
            "remove_ids": json.dumps([d["id"] for d in docs]),
        },
        files=[("files", ("replacement.txt", b"x" * (700 * 1024)))],
    )
    assert response.status_code == 202, response.text
