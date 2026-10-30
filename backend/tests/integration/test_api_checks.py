"""API проверок через HTTP: загрузка → (eager-Celery) → статус, SSE, отчёт, отметки, удаление."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from svetofor.core.config import get_settings
from svetofor.db.enums import CheckStatus, DocTypeSource
from svetofor.db.models import AuditLog, Check, Document, User
from svetofor.pipeline.ingest import IngestConfig, create_check
from svetofor.rules.store import latest_ruleset_version
from svetofor.tasks import checks
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.support.rulesets import RULE_COUNT, RULE_COUNT_44

pytestmark = pytest.mark.pg


def upload(api: dict[str, Any], name: str = "Закупка 0373.zip") -> dict[str, Any]:
    archive = build_upload(api["tmp"])
    with archive.open("rb") as stream:
        response = api["client"].post(
            "/api/checks", headers=auth(api), files={"file": (name, stream, "application/zip")}
        )
    assert response.status_code == 202, response.text
    return response.json()


def test_upload_runs_full_check_and_report(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    status = client.get(f"/api/checks/{created['check_id']}", headers=auth(api)).json()
    assert status["status"] == "done" and status["light"] == "red"
    assert status["stages"][0]["title"] == "Распаковка архива"
    assert status["ruleset"] == "44fz-supplier-base v1"
    report = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()
    card = {item["key"]: item for item in report["card"]}
    assert card["payment_term_working_days"]["display"] == "15 раб. дн."
    assert card["payment_term_working_days"]["filename"] == "Проект контракта.pdf"
    pay = next(f for f in report["findings"] if f["rule_code"] == "PAY-01")
    assert (
        pay["legal_basis"][0]["act"] == "44-ФЗ"
        and pay["why_risk"]
        and pay["rule_status"] == "draft"
    )
    assert pay["section_path"].endswith("› 2.1") and pay["highlights"]
    assert report["findings"][0]["severity"] == "red"  # критичные сверху
    # правила только для 223-ФЗ к закупке по 44-ФЗ не относятся — в отчёте их нет
    assert report["rules_summary"]["checked"] == RULE_COUNT_44
    assert report["rules_summary"]["other_law"] == RULE_COUNT - RULE_COUNT_44
    assert len(report["rules"]) == RULE_COUNT_44
    contract = next(d for d in report["documents"] if d["filename"] == "Проект контракта.pdf")
    assert (
        contract["light"] in ("red", "yellow") and contract["doc_type_title"] == "Проект контракта"
    )
    assert report["risk"]["recommendation_text"] and report["risk"]["weights_source"]


def test_rejects_bad_uploads(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = api["client"]
    response = client.post(
        "/api/checks",
        headers=auth(api),
        files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
    )
    assert (
        response.status_code == 415
        and "virus.exe» не поддерживается" in response.json()["error"]["message"]
    )
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    big = b"%PDF" + b"0" * (1024 * 1024 + 10)
    response = client.post(
        "/api/checks", headers=auth(api), files={"file": ("big.pdf", big, "application/pdf")}
    )
    assert response.status_code == 413 and response.json()["error"]["message"].startswith(
        "Файл больше 1 МБ"
    )
    assert (
        client.post("/api/checks", files={"file": ("a.pdf", b"x", "application/pdf")}).status_code
        == 401
    )


def test_history_and_filters(api: dict[str, Any]) -> None:
    first, second = upload(api), upload(api, "Вторая.zip")
    page = api["client"].get("/api/checks?page_size=1", headers=auth(api)).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert page["items"][0]["id"] == second["check_id"]  # новые сверху
    assert page["items"][0]["findings"] > 0
    green = api["client"].get("/api/checks?light=green", headers=auth(api)).json()
    assert green["total"] == 0
    assert first["check_id"] != second["check_id"]
    # подстрока; регистр кириллицы сворачивает локаль базы (в тестовой базе локаль C)
    found = api["client"].get("/api/checks?q=торая", headers=auth(api)).json()
    assert [i["id"] for i in found["items"]] == [second["check_id"]]
    literal = api["client"].get("/api/checks?q=%25", headers=auth(api)).json()
    assert literal["total"] == 0  # «%» — символ, а не шаблон «всё подряд»


def test_report_waits_for_finish(api: dict[str, Any]) -> None:
    factory = api["factory"]
    upload_file = api["tmp"] / "x.zip"
    upload_file.write_bytes(build_upload(api["tmp"]).read_bytes())
    with factory() as db:
        version = latest_ruleset_version(db, "44fz-supplier-base")
        # владелец — специалист: пакет без владельца специалисту не виден (404, а не 409)
        owner = db.scalar(select(User).where(User.email == "specialist@demo.ru"))
        assert owner is not None
        check = create_check(
            db,
            upload=upload_file,
            original_name="x.zip",
            ruleset_version_id=version.id,  # type: ignore[union-attr]
            config=IngestConfig(packages_dir=get_settings().packages_dir),
            owner_id=owner.id,
        )
        db.commit()
    response = api["client"].get(f"/api/checks/{check.id}/report", headers=auth(api))
    assert response.status_code == 409 and "ещё идёт" in response.json()["error"]["message"]


def test_limits_active_checks_per_user(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один вход не запускает проверки без меры: каждая — вызовы платной модели (LLM10)."""
    monkeypatch.setattr(get_settings(), "max_active_checks_per_user", 1)
    factory, client = api["factory"], api["client"]
    upload_file = api["tmp"] / "x.zip"
    upload_file.write_bytes(build_upload(api["tmp"]).read_bytes())
    with factory() as db:
        owner = db.scalar(select(User).where(User.email == "specialist@demo.ru"))
        assert owner is not None
        check = create_check(
            db,
            upload=upload_file,
            original_name="x.zip",
            ruleset_version_id=latest_ruleset_version(db, "44fz-supplier-base").id,  # type: ignore[union-attr]
            config=IngestConfig(packages_dir=get_settings().packages_dir),
            owner_id=owner.id,
        )
        db.commit()
    for path in ("/api/checks", "/api/checks/demo"):
        archive = build_upload(api["tmp"])
        response = client.post(
            path, headers=auth(api), files={"file": ("a.zip", archive.read_bytes())}
        )
        assert response.status_code == 429, path
        assert "уже идут проверки: 1" in response.json()["error"]["message"]
    # лимит — у каждого свой: проверка специалиста не мешает методологу
    assert upload_other(api, "methodologist").status_code == 202
    # зависшая больше суток проверка не держит лимит вечно
    with factory() as db:
        stale = db.get(Check, check.id)
        assert stale is not None
        stale.created_at = stale.created_at - timedelta(days=2)
        db.commit()
    assert upload_other(api, "specialist").status_code == 202


def upload_other(api: dict[str, Any], role: str) -> Any:
    archive = build_upload(api["tmp"])
    return api["client"].post(
        "/api/checks",
        headers=auth(api, role),
        files={"file": ("b.zip", archive.read_bytes(), "application/zip")},
    )


def test_events_stream(api: dict[str, Any]) -> None:
    created = upload(api)
    client, check_id = api["client"], created["check_id"]
    ticket = client.post(f"/api/checks/{check_id}/events/ticket", headers=auth(api)).json()
    assert ticket["expires_in"] == 120
    with client.stream("GET", f"/api/checks/{check_id}/events?ticket={ticket['ticket']}") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert "event: progress" in body and "event: done" in body
    assert client.get(f"/api/checks/{check_id}/events").status_code == 401
    # токен сессии в адресе больше не принимается — он попал бы в журналы прокси
    session = api["tokens"]["specialist"]
    assert client.get(f"/api/checks/{check_id}/events?ticket={session}").status_code == 401
    # билет — только на события этой проверки: к остальному API с ним не войти
    other = upload(api)["check_id"]
    assert client.get(f"/api/checks/{other}/events?ticket={ticket['ticket']}").status_code == 401
    bearer = {"Authorization": f"Bearer {ticket['ticket']}"}
    assert client.get(f"/api/checks/{check_id}", headers=bearer).status_code == 401


def test_rerun_document_pdf_and_type_fix(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    report = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()
    document_id = report["documents"][0]["id"]
    pdf = client.get(f"/api/documents/{document_id}/pdf", headers=auth(api))
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    patched = client.patch(
        f"/api/documents/{document_id}", headers=auth(api), json={"doc_type": "spec"}
    )
    assert (
        patched.status_code == 200
        and patched.json()["doc_type_title"] == "Описание объекта закупки"
    )
    rerun = client.post(f"/api/checks/{created['check_id']}/rerun", headers=auth(api))
    assert rerun.status_code == 202 and rerun.json()["check_id"] != created["check_id"]
    with api["factory"]() as db:
        document = db.get(Document, document_id)
        assert document is not None and document.doc_type_source is DocTypeSource.USER  # не затёрто
        assert db.scalar(select(AuditLog).where(AuditLog.action == "doc_type")) is not None
        new = db.get(Check, rerun.json()["check_id"])
        assert new is not None and new.status is CheckStatus.DONE


def test_feedback_and_missed(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    finding = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()[
        "findings"
    ][0]
    url = f"/api/findings/{finding['id']}/feedback"
    assert client.post(url, headers=auth(api), json={"verdict": "correct"}).status_code == 204
    assert (
        client.post(
            url, headers=auth(api), json={"verdict": "false_positive", "comment": "это норма"}
        ).status_code
        == 204
    )
    again = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()
    assert (
        next(f for f in again["findings"] if f["id"] == finding["id"])["feedback"]
        == "false_positive"
    )
    missed = client.post(
        f"/api/checks/{created['check_id']}/missed",
        headers=auth(api),
        json={"rule_code": "LIA-01", "comment": "штраф выше нормы"},
    )
    assert missed.status_code == 204


def test_delete_package_rights(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    url = f"/api/packages/{created['package_id']}"
    assert client.delete(url, headers=auth(api, "methodologist")).status_code == 403
    assert client.delete(url, headers=auth(api)).status_code == 204
    assert client.get(f"/api/checks/{created['check_id']}", headers=auth(api)).status_code == 404
    assert not (get_settings().packages_dir / created["package_id"]).exists()


def test_queue_unavailable(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(checks.run_check, "delay", broken)
    archive = build_upload(api["tmp"])
    with archive.open("rb") as stream:
        response = api["client"].post(
            "/api/checks", headers=auth(api), files={"file": ("a.zip", stream, "application/zip")}
        )
    assert response.status_code == 503
    with api["factory"]() as db:
        check = db.scalar(select(Check))
        assert check is not None and check.status is CheckStatus.FAILED
        assert check.error == "Очередь задач недоступна, проверка не запущена"


def test_several_files_in_one_upload(api: dict[str, Any]) -> None:
    """Документы, скачанные из ЕИС по одному, проверяются без упаковки в архив."""
    from tests.integration.test_analysis import CONTRACT, SPEC
    from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup

    client, tmp = api["client"], api["tmp"]
    contract = build_pdf(PdfSpec(pages=pages_from_markup(CONTRACT)), tmp / "c.pdf").read_bytes()
    spec = build_pdf(PdfSpec(pages=pages_from_markup(SPEC)), tmp / "s.pdf").read_bytes()
    response = client.post(
        "/api/checks",
        headers=auth(api),
        files=[
            ("files", ("Проект контракта.pdf", contract, "application/pdf")),
            ("files", ("Описание объекта закупки.pdf", spec, "application/pdf")),
            ("files", ("Описание объекта закупки.pdf", spec + b"\n%copy", "application/pdf")),
        ],
    )
    assert response.status_code == 202, response.text
    check_id = response.json()["check_id"]
    report = client.get(f"/api/checks/{check_id}/report", headers=auth(api)).json()
    names = sorted(d["filename"] for d in report["documents"])
    assert names == [
        "Описание объекта закупки (2).pdf",
        "Описание объекта закупки.pdf",
        "Проект контракта.pdf",
    ]
    assert report["package"]["source_filename"] == "Пакет из 3 файлов.zip"
    assert any(f["rule_code"] == "CON-01" for f in report["findings"])  # сверка документов


def test_upload_needs_a_supported_file(api: dict[str, Any]) -> None:
    client = api["client"]
    assert client.post("/api/checks", headers=auth(api)).status_code == 400
    bad = client.post(
        "/api/checks",
        headers=auth(api),
        files=[
            ("files", ("Проект контракта.pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("setup.exe", b"MZ", "application/octet-stream")),
        ],
    )
    assert bad.status_code == 415 and "setup.exe" in bad.json()["error"]["message"]


def test_compare_two_editions(api: dict[str, Any]) -> None:
    """Заказчик выпустил изменения: что поменялось в пунктах, карточке и рисках."""
    import zipfile

    from tests.integration.test_analysis import CONTRACT, SPEC
    from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup

    client, tmp = api["client"], api["tmp"]
    first = upload(api)["check_id"]
    edition = tmp / "edition2.zip"
    contract2 = CONTRACT.replace("в течение 15 рабочих дней", "в течение 45 рабочих дней")
    spec2 = SPEC.replace("10 календарных дней", "10 рабочих дней")
    with zipfile.ZipFile(edition, "w") as zf:
        for name, markup in (
            ("Проект контракта.pdf", contract2),
            ("Описание объекта закупки.pdf", spec2),
        ):
            zf.write(
                build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp / f"e{len(name)}.pdf"), name
            )
    with edition.open("rb") as stream:
        second = client.post(
            "/api/checks",
            headers=auth(api),
            files={"file": ("Изменения.zip", stream, "application/zip")},
        ).json()["check_id"]
    diff = client.get(f"/api/checks/{second}/compare?with={first}", headers=auth(api)).json()
    contract = next(d for d in diff["documents"] if d["new_filename"] == "Проект контракта.pdf")
    assert contract["status"] == "changed"
    (payment,) = [c for c in contract["changes"] if c["new"] and c["new"]["number"] == "2.1"]
    assert payment["status"] == "changed"
    assert {"op": "delete", "text": "15"} in payment["diff"] and {
        "op": "insert",
        "text": "45",
    } in payment["diff"]
    card = {c["key"]: c for c in diff["card"]}
    assert (
        card["payment_term_working_days"]["old"] == "15"
        and card["payment_term_working_days"]["new"] == "45"
    )
    severity = {r["rule_code"]: r for r in diff["risks"]["severity_changed"]}
    assert (
        severity["PAY-01"]["old_severity"] == "yellow" and severity["PAY-01"]["severity"] == "red"
    )
    assert "CON-01" in {r["rule_code"] for r in diff["risks"]["resolved"]}  # противоречие ушло
    same = client.get(f"/api/checks/{second}/compare?with={second}", headers=auth(api))
    assert same.status_code == 400
    word = client.get(f"/api/checks/{second}/compare.docx?with={first}", headers=auth(api))
    assert word.status_code == 200
    from tests.integration.test_api_exports import docx_text

    text = docx_text(word.content)
    assert "Изменения в документации закупки" in text and "Срок оплаты" in text
    assert "Внимание → Критично" in text  # PAY-01 сменил цвет
