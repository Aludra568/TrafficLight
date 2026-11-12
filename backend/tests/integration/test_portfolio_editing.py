"""Редактирование сохраняет историю, доступы, лимиты и идемпотентность."""

import io
import json
import uuid
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook

from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_api_access import second_specialist
from tests.integration.test_portfolios import submit

pytestmark = pytest.mark.pg


def create(api):
    payload = build_upload(api["tmp"]).read_bytes()
    response = submit(api, [("source.zip", payload)])
    assert response.status_code == 202
    return f"/api/portfolios/{response.json()['id']}", payload


def test_rename_add_remove_preserves_history(api):
    client, headers = api["client"], auth(api)
    base, payload = create(api)
    initial = client.get(base, headers=headers).json()
    assert initial["can_edit"]
    item = initial["items"][0]
    old_report = client.get(f"/api/checks/{item['check_id']}/report", headers=headers).json()
    renamed = client.patch(base, headers=headers, json={"title": "Поставки ноября", "version": 0})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title_version"] == 1
    assert (
        client.patch(base, headers=headers, json={"title": "Устаревшее", "version": 0}).status_code
        == 409
    )
    key = str(uuid.uuid4())
    for _ in range(2):
        response = client.post(
            base + "/items",
            headers=headers,
            data={"request_key": key},
            files=[("files", ("same.zip", payload))],
        )
        assert response.status_code == 202, response.text
    current = client.get(base, headers=headers).json()
    assert current["total"] == 2
    assert current["items"][0]["check_id"] == item["check_id"]
    assert (
        client.delete(base + f"/items/{item['id']}?version=0", headers=headers).status_code == 204
    )
    assert client.get(base, headers=headers).json()["total"] == 1
    assert (
        client.get(f"/api/checks/{item['check_id']}/report", headers=headers).json() == old_report
    )
    other = second_specialist(api)
    assert (
        client.post(
            base + "/items",
            headers=other,
            data={"request_key": str(uuid.uuid4())},
            files=[("files", ("x.zip", payload))],
        ).status_code
        == 404
    )


def test_documents_new_revision_and_empty_export(api):
    client, headers = api["client"], auth(api)
    base, _ = create(api)
    item = client.get(base, headers=headers).json()["items"][0]
    path = base + f"/items/{item['id']}/documents"
    docs = client.get(path, headers=headers)
    assert docs.status_code == 200, docs.text
    old_docs = docs.json()["documents"]
    keep = old_docs[0]
    remove = [d["id"] for d in old_docs[1:]]
    import json

    request = {"request_key": str(uuid.uuid4()), "version": "0", "remove_ids": json.dumps(remove)}
    changed = client.post(
        path,
        headers=headers,
        data=request,
        files=[("files", ("Дополнение.txt", "Дополнительные условия поставки".encode()))],
    )
    assert changed.status_code == 202, changed.text
    new = client.get(base, headers=headers).json()["items"][0]
    assert new["check_id"] and new["check_id"] != item["check_id"]
    assert client.get(f"/api/checks/{item['check_id']}/report", headers=headers).status_code == 200
    revised = client.get(path, headers=headers).json()
    assert len(revised["documents"]) == 2
    assert any(keep["filename"] in d["filename"] for d in revised["documents"])
    assert (
        client.post(
            path,
            headers=headers,
            data=request,
            files=[("files", ("Дополнение.txt", "Дополнительные условия поставки".encode()))],
        ).status_code
        == 202
    )
    assert client.get(base, headers=headers).json()["items"][0]["check_id"] == new["check_id"]
    assert (
        client.delete(
            base + f"/items/{item['id']}?version={new['version']}", headers=headers
        ).status_code
        == 204
    )
    empty = client.get(base, headers=headers).json()
    assert empty["total"] == 0 and empty["light"] == "gray"
    exported = client.get(base + "/export.xlsx", headers=headers)
    assert exported.status_code == 200
    assert load_workbook(io.BytesIO(exported.content))["Замечания"].max_row == 1


def test_pending_edit_reserves_quota_and_has_no_deleted_error(api, monkeypatch):
    from svetofor.core.config import get_settings
    from svetofor.db.models import Check
    from svetofor.db.portfolio import PortfolioItem

    client, headers = api["client"], auth(api)
    base, _ = create(api)
    item = client.get(base, headers=headers).json()["items"][0]
    with api["factory"]() as db:
        entry = db.get(PortfolioItem, uuid.UUID(item["id"]))
        entry.source_bytes = 100 * 1024
        assert db.get(Check, entry.check_id)
        db.commit()
    monkeypatch.setattr(get_settings(), "max_portfolio_mb", 1)
    monkeypatch.setattr("svetofor.api.routers.portfolio_documents.dispatch_edit", lambda *_: None)
    docs = client.get(base + f"/items/{item['id']}/documents", headers=headers).json()["documents"]
    changed = client.post(
        base + f"/items/{item['id']}/documents",
        headers=headers,
        data={
            "request_key": str(uuid.uuid4()),
            "version": "0",
            "remove_ids": json.dumps([d["id"] for d in docs]),
        },
        files=[("files", ("addition.txt", b"a" * (700 * 1024)))],
    )
    assert changed.status_code == 202, changed.text
    pending = client.get(base, headers=headers).json()["items"][0]
    assert pending["editing"] and pending["error"] is None
    response = client.post(
        base + "/items",
        headers=headers,
        data={"request_key": str(uuid.uuid4())},
        files=[("files", ("another.zip", b"a" * (400 * 1024)))],
    )
    assert response.status_code == 413, response.text
    assert client.get(base, headers=headers).json()["total"] == 1


def test_bundle_retains_long_file_suffix_and_export_names(api):
    from tests.support.pdfgen import L, PdfSpec, build_pdf

    client, headers = api["client"], auth(api)
    base, _ = create(api)
    long_name = "Д" * 160 + ".pdf"
    pdf1 = build_pdf(
        PdfSpec(pages=[[L("ПРОЕКТ КОНТРАКТА"), L("Аванс не предусмотрен.")]]),
        api["tmp"] / "one.pdf",
    ).read_bytes()
    pdf2 = build_pdf(
        PdfSpec(pages=[[L("ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ"), L("Количество товара 500 штук.")]]),
        api["tmp"] / "two.pdf",
    ).read_bytes()
    response = client.post(
        base + "/items",
        headers=headers,
        data={"request_key": str(uuid.uuid4()), "bundle": "true"},
        files=[("files", (long_name, pdf1)), ("files", ("spec.pdf", pdf2))],
    )
    assert response.status_code == 202, response.text
    data = client.get(base, headers=headers).json()
    assert data["total"] == 2 and data["done"] == 2, data
    item = data["items"][1]
    docs = client.get(base + f"/items/{item['id']}/documents", headers=headers).json()["documents"]
    assert len(docs) == 2 and all(d["filename"].endswith(".pdf") for d in docs)
    renamed = client.patch(base, headers=headers, json={"title": "Поставки / ноября", "version": 0})
    assert renamed.status_code == 200
    for extension, prefix in (("xlsx", "Чек-лист"), ("docx", "Отчёт")):
        exported = client.get(base + f"/export.{extension}", headers=headers)
        assert exported.status_code == 200
        header = unquote(exported.headers["content-disposition"])
        assert f"{prefix} по «Поставки _ ноября».{extension}" in header


def test_failed_preparation_preserves_report_and_retries(api, monkeypatch):
    from svetofor.db.portfolio import PortfolioItem

    client, headers = api["client"], auth(api)
    base, _ = create(api)
    item = client.get(base, headers=headers).json()["items"][0]
    target = base + f"/items/{item['id']}/documents"
    request = {"request_key": str(uuid.uuid4()), "version": "0"}
    with api["factory"]() as db:
        original_size = db.get(PortfolioItem, uuid.UUID(item["id"])).source_bytes

    def broken(*_):
        raise OSError("synthetic storage failure")

    with monkeypatch.context() as local:
        local.setattr("svetofor.tasks.portfolio_edits.assemble", broken)
        failed = client.post(
            target,
            headers=headers,
            data=request,
            files=[("files", ("new.txt", b"additional terms"))],
        )
        assert failed.status_code == 202
    current = client.get(base, headers=headers).json()["items"][0]
    assert current["check_id"] == item["check_id"] and current["edit_error"]
    with api["factory"]() as db:
        assert db.get(PortfolioItem, uuid.UUID(item["id"])).source_bytes == original_size
    response = client.post(
        target, headers=headers, data=request, files=[("files", ("new.txt", b"additional terms"))]
    )
    assert response.status_code == 202, response.text
    current = client.get(base, headers=headers).json()["items"][0]
    assert current["check_id"] != item["check_id"] and not current["edit_error"]


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_additions_are_serialized(api, monkeypatch, same_key):
    from concurrent.futures import ThreadPoolExecutor

    base, _ = create(api)
    headers = auth(api)
    dispatches = []
    monkeypatch.setattr(
        "svetofor.api.routers.portfolio_editing.enqueue", lambda *args: dispatches.append(args[0])
    )
    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    if same_key:
        keys[1] = keys[0]

    def add(key):
        return api["client"].post(
            base + "/items",
            headers=headers,
            data={"request_key": key},
            files=[("files", ("same.zip", b"queued package"))],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(add, keys))
    assert [r.status_code for r in responses] == [202, 202]
    data = api["client"].get(base, headers=headers).json()
    assert data["total"] == (2 if same_key else 3)
    assert len(dispatches) == (1 if same_key else 2)
    assert len({i["position"] for i in data["items"]}) == data["total"]


def test_edit_permissions_and_foreign_documents(api):
    client, headers = api["client"], auth(api)
    base, _ = create(api)
    item = client.get(base, headers=headers).json()["items"][0]
    methodologist = auth(api, "methodologist")
    assert client.get(base, headers=methodologist).json()["can_edit"] is False
    assert (
        client.patch(
            base, headers=methodologist, json={"title": "Чужое изменение", "version": 0}
        ).status_code
        == 403
    )
    target = base + f"/items/{item['id']}/documents"
    docs = client.get(target, headers=headers).json()["documents"]
    removed = client.post(
        target,
        headers=headers,
        data={
            "request_key": str(uuid.uuid4()),
            "version": "0",
            "remove_ids": json.dumps([d["id"] for d in docs]),
        },
    )
    assert removed.status_code == 400
    other_base, _ = create(api)
    other = client.get(other_base, headers=headers).json()["items"][0]
    other_docs = client.get(other_base + f"/items/{other['id']}/documents", headers=headers).json()
    rejected = client.post(
        target,
        headers=headers,
        data={
            "request_key": str(uuid.uuid4()),
            "version": "0",
            "remove_ids": json.dumps([other_docs["documents"][0]["id"]]),
        },
    )
    assert rejected.status_code == 404
    assert client.get(base, headers=headers).json()["items"][0]["check_id"] == item["check_id"]
    assert (
        client.patch(
            base, headers=auth(api, "admin"), json={"title": "Разрешённое изменение", "version": 0}
        ).status_code
        == 200
    )
