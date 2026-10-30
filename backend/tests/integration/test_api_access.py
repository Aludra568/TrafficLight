"""Разграничение доступа (тест на проникновение 30.10: P-01…P-04, H9, L6).

Специалист видит и меняет только свои пакеты; методолог и администратор — все. Чужое для
специалиста — 404, как несуществующее. Песочница правил — только методологу, и результат
задачи — только тому, кто её запустил. Описание API выключается настройкой API_DOCS.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from svetofor.api.security import create_access_token, hash_password
from svetofor.core.config import get_settings
from svetofor.db.enums import Role
from svetofor.db.models import Check, Package, User
from svetofor.pipeline.ingest import IngestConfig, create_check
from svetofor.rules.store import latest_ruleset_version
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_api_checks import upload

pytestmark = pytest.mark.pg

SANDBOX = {"rule_code": "PAY-01", "text": "Оплата в течение 30 рабочих дней с даты приёмки."}


def second_specialist(api: dict[str, Any]) -> dict[str, str]:
    """Второй специалист той же организации — чужие пакеты ему не видны."""
    with api["factory"]() as db:
        user = User(
            email="specialist2@demo.ru",
            full_name="Второй специалист",
            role=Role.SPECIALIST,
            password_hash=hash_password("другой-пароль-для-теста"),
        )
        db.add(user)
        db.commit()
        token = create_access_token(user.id, Role.SPECIALIST)
    return {"Authorization": f"Bearer {token}"}


def test_specialist_sees_only_own_packages(api: dict[str, Any]) -> None:
    client = api["client"]
    first, second = upload(api), upload(api)
    check_id = first["check_id"]
    report = client.get(f"/api/checks/{check_id}/report", headers=auth(api)).json()
    document_id, finding_id = report["documents"][0]["id"], report["findings"][0]["id"]
    other = second_specialist(api)

    history = client.get("/api/checks", headers=other).json()
    assert history["total"] == 0 and history["items"] == []
    assert client.get("/api/checks", headers=auth(api)).json()["total"] == 2
    for role in ("methodologist", "admin"):  # методологу нужны результаты, админу — стенд
        assert client.get("/api/checks", headers=auth(api, role)).json()["total"] == 2
        assert client.get(f"/api/checks/{check_id}", headers=auth(api, role)).status_code == 200
        assert (
            client.get(f"/api/checks/{check_id}/report", headers=auth(api, role)).status_code == 200
        )
        pdf = client.get(f"/api/documents/{document_id}/pdf", headers=auth(api, role))
        assert pdf.status_code == 200

    base = f"/api/checks/{check_id}"
    compare = f"?with={second['check_id']}"
    foreign = [
        ("GET", base, None),
        ("GET", f"{base}/report", None),
        ("GET", f"{base}/editions", None),
        ("GET", f"{base}/compare{compare}", None),
        ("GET", f"{base}/compare.docx{compare}", None),
        ("POST", f"{base}/rerun", None),
        ("POST", f"{base}/export?format=docx", None),
        ("POST", f"{base}/export?format=pdf", None),
        ("GET", f"{base}/export/pdf", None),
        ("POST", f"{base}/clarification", {}),
        ("POST", f"{base}/events/ticket", None),
        ("GET", f"{base}/events", None),
        ("POST", f"{base}/missed", {"rule_code": "LIA-01", "comment": "чужое"}),
        ("GET", f"/api/documents/{document_id}/pdf", None),
        ("PATCH", f"/api/documents/{document_id}", {"doc_type": "spec"}),
        ("POST", f"/api/findings/{finding_id}/feedback", {"verdict": "false_positive"}),
        ("DELETE", f"/api/packages/{first['package_id']}", None),
    ]
    for method, url, body in foreign:
        response = client.request(method, url, headers=other, json=body)
        assert response.status_code == 404, (method, url, response.text)
        assert response.json()["error"]["code"] == "not_found", (method, url)

    # ничего не изменилось: тип документа, отметки, пакет на месте, перепроверки нет
    with api["factory"]() as db:
        assert db.get(Package, first["package_id"]) is not None
        checks = db.scalars(select(Check).where(Check.package_id == first["package_id"])).all()
        assert len(checks) == 1
    again = client.get(f"/api/checks/{check_id}/report", headers=auth(api)).json()
    assert again["documents"][0]["doc_type"] == report["documents"][0]["doc_type"]
    assert all(f["feedback"] is None for f in again["findings"])


def test_compare_with_foreign_check_is_not_found(api: dict[str, Any]) -> None:
    """Своя проверка «с» чужой — тоже 404: сравнение показало бы чужой текст."""
    client = api["client"]
    mine = upload(api)["check_id"]
    archive = build_upload(api["tmp"])
    theirs = client.post(
        "/api/checks",
        headers=auth(api, "methodologist"),
        files={"file": ("b.zip", archive.read_bytes(), "application/zip")},
    ).json()["check_id"]
    response = client.get(f"/api/checks/{mine}/compare?with={theirs}", headers=auth(api))
    assert response.status_code == 404
    # методолог видит обе
    assert (
        client.get(
            f"/api/checks/{theirs}/compare?with={mine}", headers=auth(api, "methodologist")
        ).status_code
        == 200
    )


def test_events_with_bearer_token_check_access(api: dict[str, Any]) -> None:
    created = upload(api)
    url = f"/api/checks/{created['check_id']}/events"
    assert api["client"].get(url, headers=second_specialist(api)).status_code == 404
    with api["client"].stream("GET", url, headers=auth(api)) as own:
        assert own.status_code == 200 and "event: done" in "".join(own.iter_text())


def test_package_without_owner_only_for_methodologist_and_admin(api: dict[str, Any]) -> None:
    upload_file = api["tmp"] / "x.zip"
    upload_file.write_bytes(build_upload(api["tmp"]).read_bytes())
    with api["factory"]() as db:
        version = latest_ruleset_version(db, "44fz-supplier-base")
        assert version is not None
        check = create_check(
            db,
            upload=upload_file,
            original_name="x.zip",
            ruleset_version_id=version.id,
            config=IngestConfig(packages_dir=get_settings().packages_dir),
        )
        db.commit()
    client, url = api["client"], f"/api/checks/{check.id}"
    assert client.get(url, headers=auth(api)).status_code == 404
    assert client.get("/api/checks", headers=auth(api)).json()["total"] == 0
    assert client.get(url, headers=auth(api, "methodologist")).status_code == 200
    assert client.get(url, headers=auth(api, "admin")).status_code == 200


def test_sandbox_only_for_methodologist(api: dict[str, Any]) -> None:
    """P-01: специалист не запускает вызовы модели в обход лимита проверок."""
    client = api["client"]
    response = client.post("/api/rules/test", headers=auth(api), json=SANDBOX)
    assert response.status_code == 403
    for role in ("methodologist", "admin"):
        started = client.post("/api/rules/test", headers=auth(api, role), json=SANDBOX)
        assert started.status_code == 202, role


def test_sandbox_result_only_for_who_started_it(api: dict[str, Any]) -> None:
    """P-04: результат по номеру задачи получает только тот, кто её запустил."""
    client = api["client"]
    task_id = client.post(
        "/api/rules/test", headers=auth(api, "methodologist"), json=SANDBOX
    ).json()["task_id"]
    own = client.get(f"/api/rules/test/{task_id}", headers=auth(api, "methodologist"))
    assert own.status_code == 200 and own.json()["status"] == "done"
    assert client.get(f"/api/rules/test/{task_id}", headers=auth(api, "admin")).status_code == 404
    assert client.get(f"/api/rules/test/{task_id}", headers=auth(api)).status_code == 403
    celery_id, _, signature = task_id.rpartition(".")
    forged = f"{celery_id}.{'0' * len(signature)}"
    for bad in (forged, celery_id, "нет-такой"):
        response = client.get(f"/api/rules/test/{bad}", headers=auth(api, "methodologist"))
        assert response.status_code == 404, bad


def test_api_docs_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """P-02: вне демонстрации API_DOCS=false — описание API не отдаётся совсем."""
    from svetofor.api.main import create_app

    settings = get_settings()
    monkeypatch.setattr(settings, "api_docs", False)
    hidden = create_app()
    client = TestClient(hidden)
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404
    # схема для backend/openapi.json (npm run api-types) строится и без адреса
    assert "/api/checks" in hidden.openapi()["paths"]
    monkeypatch.setattr(settings, "api_docs", True)
    shown = TestClient(create_app())
    assert shown.get("/api/docs").status_code == 200
    assert shown.get("/api/openapi.json").status_code == 200
