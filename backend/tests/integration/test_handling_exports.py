"""Статусы сохраняются, риск остаётся, выгрузка безопасна и воспроизводима."""

from __future__ import annotations

import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from svetofor.db.enums import FindingStatus
from svetofor.db.models import Check, Finding
from svetofor.reports.export import report_paths
from tests.integration.conftest import auth
from tests.integration.test_api_access import second_specialist
from tests.integration.test_api_checks import upload
from tests.integration.test_api_exports import docx_text

pytestmark = pytest.mark.pg


def test_concurrent_status_updates_keep_versions_and_export_revision(api: dict[str, Any]) -> None:
    created = upload(api)
    base = f"/api/checks/{created['check_id']}"
    findings = api["client"].get(base + "/report", headers=auth(api)).json()["findings"][:2]

    def change(finding):
        return (
            api["client"]
            .patch(
                f"/api/findings/{finding['id']}/handling",
                headers=auth(api),
                json={"status": "resolved", "version": 0},
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(change, findings)) == [200, 200]
    with api["factory"]() as db:
        check = db.get(Check, uuid.UUID(created["check_id"]))
        assert check.counters["handling_revision"] == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(change, [findings[0], findings[0]]))
    assert statuses == [409, 409]


def test_handling_saved_conflict_and_risk_unchanged(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    base = f"/api/checks/{created['check_id']}"
    original = client.get(base + "/report", headers=auth(api)).json()
    finding = original["findings"][0]
    url = f"/api/findings/{finding['id']}/handling"
    assert finding["handling_status"] == "new" and finding["handling_version"] == 0
    for version, state in enumerate(("in_progress", "resolved", "not_applicable", "new")):
        result = client.patch(url, headers=auth(api), json={"status": state, "version": version})
        assert result.status_code == 200, result.text
        assert result.json()["version"] == version + 1
        current = client.get(base + "/report", headers=auth(api)).json()
        selected = next(f for f in current["findings"] if f["id"] == finding["id"])
        assert selected["handling_status"] == state
        assert current["check"]["light"] == original["check"]["light"]
        assert current["risk"] == original["risk"]
        assert selected["quote"] == finding["quote"]
        assert len(current["findings"]) == len(original["findings"])
    assert (
        client.patch(url, headers=auth(api), json={"status": "resolved", "version": 0}).status_code
        == 409
    )
    assert (
        client.patch(url, headers=auth(api), json={"status": "green", "version": 4}).status_code
        == 422
    )
    assert (
        client.patch(
            url, headers=second_specialist(api), json={"status": "resolved", "version": 4}
        ).status_code
        == 404
    )
    with api["factory"]() as db:
        check = db.get(Check, uuid.UUID(created["check_id"]))
        assert "-h4" in report_paths(check)[1].name


def test_xlsx_matches_report_and_workflow_is_in_word(api: dict[str, Any]) -> None:
    created = upload(api)
    client = api["client"]
    base = f"/api/checks/{created['check_id']}"
    report = client.get(base + "/report", headers=auth(api)).json()
    finding = report["findings"][0]
    changed = client.patch(
        f"/api/findings/{finding['id']}/handling",
        headers=auth(api),
        json={"status": "resolved", "version": 0},
    )
    assert changed.status_code == 200
    response = client.get(base + "/export.xlsx", headers=auth(api))
    assert response.status_code == 200, response.text
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["Замечания", "Сводка"]
    sheet = workbook["Замечания"]
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref
    assert sheet.max_row == len(report["findings"]) + 1
    assert sheet.max_column == 15
    assert sheet["E2"].value == "Критично" and sheet["E2"].fill.fgColor.rgb.endswith("FCE4E7")
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert next(row for row in rows if row[1] == finding["id"])[11] == "Отработано"
    assert all(
        sheet.cell(n, 14).hyperlink.target.startswith("http://localhost:8080/checks/")
        for n in range(2, sheet.max_row + 1)
    )
    summary = {row[0]: row[1] for row in workbook["Сводка"].iter_rows(values_only=True)}
    assert summary["Замечаний"] == len(report["findings"]) and summary["Отработано"] == 1
    word = client.post(base + "/export", headers=auth(api))
    assert word.status_code == 200 and "Обработка: Отработано" in docx_text(word.content)
    assert client.get(base + "/export.xlsx", headers=second_specialist(api)).status_code == 404


def test_untrusted_text_does_not_become_excel_formula(api: dict[str, Any]) -> None:
    created = upload(api)
    with api["factory"]() as db:
        findings = list(
            db.scalars(
                select(Finding).where(
                    Finding.check_id == uuid.UUID(created["check_id"]),
                    Finding.status == FindingStatus.SHOWN,
                )
            )
        )
        for finding, prefix in zip(findings, ("=", "+", "-", "@"), strict=False):
            finding.title = prefix + "SUM(1,2)"
            finding.quote = prefix + 'HYPERLINK("https://example.invalid")'
            finding.related = {**(finding.related or {}), "evidence": []}
        db.commit()
    response = api["client"].get(
        f"/api/checks/{created['check_id']}/export.xlsx", headers=auth(api)
    )
    workbook = load_workbook(io.BytesIO(response.content), data_only=False)
    assert all(cell.data_type != "f" for sheet in workbook for row in sheet for cell in row)
    assert any(str(cell.value).startswith("=SUM") for row in workbook["Замечания"] for cell in row)
