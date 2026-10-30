"""API правил: новое правило без кода → сразу работает в проверке; версии, песочница, наборы."""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.integration.conftest import auth
from tests.integration.test_api_checks import upload
from tests.support.fake_models import fact
from tests.support.rulesets import RULE_COUNT, RULE_COUNT_44, STARTER_RULES

pytestmark = pytest.mark.pg

NEW_RULE: dict[str, Any] = {
    "id": "TST-01",
    "title": "Пеня в процентах от цены контракта",
    "category": "liability",
    "law": ["44-ФЗ"],
    "applies_to": ["contract"],
    "type": "pattern",
    "patterns": [r"(?i)пен[юяи]\s+в\s+размере\s+\d+/\d+\s+цены"],
    "severity": "red",
    "message": "Пеня считается от цены контракта: {quote}",
    "why_risk": "Пеня от цены контракта, а не от неисполненной части, многократно выше законной.",
    "recommendation": "Направить запрос на разъяснение порядка начисления пени.",
}


def wait_sandbox(api: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    client = api["client"]
    # песочница вызывает модель — только методолог (специалист получает 403)
    task = client.post("/api/rules/test", headers=auth(api, "methodologist"), json=body)
    assert task.status_code == 202, task.text
    for _ in range(50):
        result = client.get(
            f"/api/rules/test/{task.json()['task_id']}", headers=auth(api, "methodologist")
        ).json()
        if result["status"] != "pending":
            return result
        time.sleep(0.05)
    raise AssertionError("песочница не ответила")


def test_list_and_detail(api: dict[str, Any]) -> None:
    client = api["client"]
    rules = client.get("/api/rules", headers=auth(api)).json()
    assert len(rules) == RULE_COUNT
    pay = next(r for r in rules if r["code"] == "PAY-01")
    assert pay["severity"] == "red" and pay["status"] == "draft" and pay["category_title"]
    patterns = client.get("/api/rules?type=pattern", headers=auth(api)).json()
    assert [r["code"] for r in patterns] == sorted(
        r.id for r in STARTER_RULES if r.type == "pattern"
    )
    assert {r["code"] for r in client.get("/api/rules?q=оплат", headers=auth(api)).json()} >= {
        "PAY-01"
    }
    detail = client.get("/api/rules/PAY-01", headers=auth(api)).json()
    assert detail["body"]["fact"] == "payment_term_working_days" and len(detail["versions"]) == 1
    assert client.get("/api/rules/NOPE-01", headers=auth(api)).status_code == 404


def test_new_rule_without_code_works_in_next_check(api: dict[str, Any]) -> None:
    client = api["client"]
    assert client.post("/api/rules", headers=auth(api), json={"rule": NEW_RULE}).status_code == 403
    saved = client.post("/api/rules", headers=auth(api, "methodologist"), json={"rule": NEW_RULE})
    assert saved.status_code == 201, saved.text
    assert saved.json() == {"code": "TST-01", "version": 1, "ruleset_version": 2}
    created = upload(api)
    report = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()
    assert report["rules_summary"]["checked"] == RULE_COUNT_44 + 1  # новое правило — уже в проверке
    finding = next(f for f in report["findings"] if f["rule_code"] == "TST-01")
    assert (
        finding["quote"].startswith("4.1. За просрочку поставщик уплачивает пеню")
        and finding["highlights"]
    )
    duplicate = client.post(
        "/api/rules", headers=auth(api, "methodologist"), json={"rule": NEW_RULE}
    )
    assert duplicate.status_code == 409


def test_invalid_rule_explained(api: dict[str, Any]) -> None:
    bad = {**NEW_RULE, "patterns": ["(незакрытая скобка"], "title": "x"}
    response = api["client"].post(
        "/api/rules", headers=auth(api, "methodologist"), json={"rule": bad}
    )
    assert response.status_code == 422
    problems = response.json()["error"]["details"]["problems"]
    assert any("TST-01" in p and "title" in p for p in problems)
    assert any("регулярном выражении" in p for p in problems)
    no_type = {k: v for k, v in NEW_RULE.items() if k != "type"}
    response = api["client"].post(
        "/api/rules", headers=auth(api, "methodologist"), json={"rule": no_type}
    )
    assert "укажите type" in response.json()["error"]["details"]["problems"][0]


def test_versions_and_verification(api: dict[str, Any]) -> None:
    client = api["client"]
    body = client.get("/api/rules/PAY-01", headers=auth(api)).json()["body"]
    changed = {**body, "title": "Срок оплаты дольше семи рабочих дней"}
    saved = client.put(
        "/api/rules/PAY-01",
        headers=auth(api, "methodologist"),
        json={"rule": changed, "change_note": "формулировка"},
    )
    assert saved.json()["version"] == 2
    mismatch = client.put(
        "/api/rules/PAY-01",
        headers=auth(api, "methodologist"),
        json={"rule": {**changed, "id": "PAY-09"}},
    )
    assert mismatch.status_code == 400
    verified = client.post(
        "/api/rules/PAY-01/verify",
        headers=auth(api, "methodologist"),
        json={"verified_by": "Иванова А. А., юрист"},
    )
    assert verified.json()["version"] == 3
    detail = client.get("/api/rules/PAY-01", headers=auth(api)).json()
    assert (
        detail["body"]["status"] == "verified"
        and detail["body"]["verified_by"] == "Иванова А. А., юрист"
    )
    assert [v["version"] for v in detail["versions"]] == [3, 2, 1]
    assert detail["versions"][1]["change_note"] == "формулировка"
    assert detail["versions"][1]["author"] == "methodologist@demo.ru"


def test_sandbox_for_each_rule_type(api: dict[str, Any]) -> None:
    pattern = wait_sandbox(
        api,
        {
            "rule_code": "VOL-01",
            "text": "Поставка товара осуществляется по мере необходимости Заказчика.",
        },
    )
    assert pattern["status"] == "done" and pattern["result"]["outcome"] == "violation"
    assert "по мере необходимости" in pattern["result"]["findings"][0]["quote"]
    text = (
        "9.1. За просрочку поставщик уплачивает пеню в размере 1/100 цены контракта "
        "за каждый день просрочки."
    )
    llm = wait_sandbox(api, {"rule_code": "LIA-03", "text": text})
    assert llm["result"]["outcome"] == "violation" and llm["result"]["findings"][0]["explanation"]
    api["backend"].facts["Проверяемый текст"] = [
        fact("payment_term_working_days", 30, "в течение 30 рабочих дней", "working")
    ]
    threshold = wait_sandbox(
        api,
        {
            "rule_code": "PAY-01",
            "text": "2.1. Оплата производится в течение 30 рабочих дней с даты подписания "
            "документа о приёмке.",
        },
    )
    assert threshold["result"]["outcome"] == "violation"
    assert threshold["result"]["facts"][0]["display"] == "30"
    draft = wait_sandbox(api, {"rule": NEW_RULE, "text": text})
    assert draft["result"]["outcome"] == "violation"  # черновик проверяется до сохранения
    cross = wait_sandbox(api, {"rule_code": "CON-01", "text": text})
    assert cross["result"]["outcome"] == "not_checked" and "на пакете" in cross["result"]["reason"]


def test_ruleset_export_import(api: dict[str, Any]) -> None:
    client = api["client"]
    rulesets = client.get("/api/rulesets", headers=auth(api)).json()
    assert rulesets[0]["code"] == "44fz-supplier-base" and rulesets[0]["rules"] == RULE_COUNT
    exported = client.get("/api/rulesets/44fz-supplier-base/export", headers=auth(api))
    assert (
        exported.headers["content-type"].startswith("application/x-yaml")
        and "PAY-01" in exported.text
    )
    edited = exported.text.replace(
        "Срок оплаты дольше 7 рабочих дней", "Оплата позже 7 рабочих дней"
    )
    imported = client.post(
        "/api/rulesets/import",
        headers=auth(api, "methodologist"),
        files={"file": ("rules.yaml", edited.encode(), "application/x-yaml")},
    )
    assert imported.status_code == 200 and imported.json()["version"] == 2
    broken = client.post(
        "/api/rulesets/import",
        headers=auth(api, "methodologist"),
        files={"file": ("bad.yaml", b"ruleset: [", "application/x-yaml")},
    )
    assert broken.status_code == 422 and broken.json()["error"]["details"]["problems"]


def test_quality_reflects_feedback(api: dict[str, Any]) -> None:
    client = api["client"]
    created = upload(api)
    findings = client.get(f"/api/checks/{created['check_id']}/report", headers=auth(api)).json()[
        "findings"
    ]
    pay = next(f for f in findings if f["rule_code"] == "PAY-01")
    client.post(
        f"/api/findings/{pay['id']}/feedback", headers=auth(api), json={"verdict": "correct"}
    )
    quality = client.get("/api/quality", headers=auth(api)).json()
    stats = next(s for s in quality["feedback"] if s["rule_code"] == "PAY-01")
    assert stats["correct"] == 1 and stats["precision"] == 1.0
    assert quality["quotes"]["shown"] == len(findings) and quality["eval"] is None
    rules = client.get("/api/rules?q=PAY-01", headers=auth(api)).json()
    assert rules[0]["triggers_30d"] == 1 and rules[0]["precision"] == 1.0
