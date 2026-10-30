"""Враждебный и случайно испорченный ввод: понятный ответ 4xx/503, а не «внутренняя ошибка».

Найдено аудитом 2026-10-08: символ NUL в строке и огромный номер страницы давали 500 (их не
принимает PostgreSQL), ошибки разбора формы приходили по-английски, недоступная очередь при
выгрузке PDF и в песочнице правил — 500, оборванная загрузка оставляла файл на диске.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from svetofor.api.routers import checks as checks_router
from svetofor.core.config import get_settings
from tests.integration.conftest import auth
from tests.integration.test_api_checks import upload

pytestmark = pytest.mark.pg


def _client(api: dict[str, Any]) -> TestClient:
    """Клиент, который видит ответы на необработанные ошибки, как браузер."""
    return TestClient(api["client"].app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/checks", {"params": {"q": "ТЗ\x00"}}),
        ("post", "/api/auth/login", {"json": {"email": "a\x00@demo.ru", "password": "x"}}),
    ],
)
def test_nul_in_input_is_bad_request(
    api: dict[str, Any], method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = getattr(_client(api), method)(path, headers=auth(api), **kwargs)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_value"


def test_nul_in_comment_is_bad_request(api: dict[str, Any]) -> None:
    check = upload(api)["check_id"]
    response = _client(api).post(
        f"/api/checks/{check}/missed", headers=auth(api), json={"comment": "пропущено\x00"}
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_value"


def test_page_number_is_bounded(api: dict[str, Any]) -> None:
    response = _client(api).get("/api/checks", headers=auth(api), params={"page": 10**18})
    assert response.status_code == 422


def test_multipart_errors_are_in_russian(api: dict[str, Any]) -> None:
    files = [
        ("files", (f"{i}.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")) for i in range(1001)
    ]
    response = _client(api).post("/api/checks", headers=auth(api), files=files)
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "bad_upload",
        "message": "Слишком много файлов в одной загрузке — не больше 1000",
    }


def test_queue_outage_on_pdf_export_is_503(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from svetofor.tasks.exports import export_pdf_task

    check = upload(api)["check_id"]

    def broker_down(*_: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(export_pdf_task, "delay", broker_down)
    response = _client(api).post(f"/api/checks/{check}/export?format=pdf", headers=auth(api))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_unavailable"


def test_queue_outage_in_rule_sandbox_is_503(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from svetofor.tasks.rules import sandbox_task

    def broker_down(*_: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(sandbox_task, "delay", broker_down)
    rules = _client(api).get("/api/rules", headers=auth(api)).json()
    response = _client(api).post(
        "/api/rules/test",
        headers=auth(api, "methodologist"),  # песочница — только методологу
        json={"rule_code": rules[0]["code"], "text": "Оплата в течение 30 дней с даты приёмки."},
    )
    assert response.status_code == 503


@pytest.mark.anyio
async def test_interrupted_upload_leaves_no_file(api: dict[str, Any]) -> None:
    class Broken:
        filename = "Пакет.zip"
        sent = False

        async def read(self, _: int) -> bytes:
            if self.sent:
                raise ClientDisconnect
            self.sent = True
            return b"PK" + b"x" * 100

    incoming = get_settings().packages_dir / "_incoming"
    target = incoming / "partial.zip"
    with pytest.raises(ClientDisconnect):
        await checks_router._save_upload(Broken(), target, 10**6)  # type: ignore[arg-type]
    assert not target.exists()
    bundle = incoming / "bundle.zip"
    with pytest.raises(ClientDisconnect):
        await checks_router._save_bundle([Broken(), Broken()], bundle, 10**6)  # type: ignore[list-item]
    assert not bundle.exists()
