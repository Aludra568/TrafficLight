"""Сохранённые замечания не исчезают после последующего сбоя проверки."""

import io
import uuid

import pytest
from openpyxl import load_workbook

from svetofor.db.enums import CheckStatus, FindingStatus, Severity
from svetofor.db.models import Check, Finding
from svetofor.pipeline.finalize import fail_check
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload
from tests.integration.test_api_access import second_specialist
from tests.integration.test_api_exports import docx_text
from tests.integration.test_portfolios import submit

pytestmark = pytest.mark.pg


@pytest.mark.parametrize("keep_red", [True, False])
def test_failed_check_keeps_saved_findings(api, keep_red):
    from sqlalchemy import select

    response = submit(api, [("partial.zip", build_upload(api["tmp"]).read_bytes())])
    base = f"/api/portfolios/{response.json()['id']}"
    client, headers = api["client"], auth(api)
    check_id = client.get(base, headers=headers).json()["items"][0]["check_id"]
    with api["factory"]() as db:
        check = db.get(Check, uuid.UUID(check_id))
        check.status, check.light, check.finished_at = CheckStatus.RUNNING, None, None
        findings = list(
            db.scalars(
                select(Finding).where(
                    Finding.check_id == check.id,
                    Finding.status == FindingStatus.SHOWN,
                )
            )
        )
        assert findings
        if not keep_red:
            for finding in findings:
                finding.severity = Severity.YELLOW
        expected = {str(f.id) for f in findings}
        db.commit()
    fail_check(api["factory"], uuid.UUID(check_id), "Сбой после сохранения замечаний")
    data = client.get(base, headers=headers).json()
    assert data["failed"] == 1 and data["done"] == 0
    assert data["light"] == ("red" if keep_red else "gray")
    assert {f["id"] for f in data["findings"]} == expected
    assert data["items"][0]["error"]
    other = second_specialist(api)
    for url in (base + "/export.xlsx", f"/api/checks/{check_id}/export.xlsx"):
        exported = client.get(url, headers=headers)
        assert exported.status_code == 200
        book = load_workbook(io.BytesIO(exported.content))
        summary = {r[0]: r[1] for r in book["Сводка"].iter_rows(values_only=True)}
        assert summary.get("С ошибкой", summary.get("Проверок с ошибкой")) == 1
        assert {r[1] for r in book["Замечания"].iter_rows(min_row=2, values_only=True)} == expected
        assert client.get(url, headers=other).status_code == 404
    word = client.get(base + "/export.docx", headers=headers)
    assert word.status_code == 200 and "Сбой после сохранения" in docx_text(word.content)
