"""Загрузка закупки из ЕИС через API: сеть подменена, Celery — eager."""

from __future__ import annotations

import zipfile
from typing import Any

import pytest

from svetofor.core.config import get_settings
from svetofor.db.models import Check, Package
from svetofor.pipeline.eis import EisError, EisPackage
from svetofor.tasks import eis as eis_task
from tests.integration.conftest import auth
from tests.integration.test_analysis import build_upload

pytestmark = pytest.mark.pg
N44 = "0362100026126000023"


def fake_fetch(api: dict[str, Any], seen: list[str]) -> Any:
    def fetch(number: str, *, max_files: int, max_bytes: int) -> EisPackage:
        seen.append(number)
        with zipfile.ZipFile(build_upload(api["tmp"])) as archive:
            files = [(name, archive.read(name)) for name in archive.namelist()]
        return EisPackage("44-ФЗ", number, files)

    return fetch


def start(api: dict[str, Any], number: str, role: str = "specialist") -> Any:
    return api["client"].post("/api/checks/eis", headers=auth(api, role), json={"number": number})


def test_number_to_finished_check(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(eis_task, "fetch_package", fake_fetch(api, seen))
    response = start(api, f"№ {N44[:4]} {N44[4:]}")
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    status = api["client"].get(f"/api/checks/eis/{task_id}", headers=auth(api)).json()
    assert status["status"] == "done" and seen == [N44]
    check = api["client"].get(f"/api/checks/{status['check_id']}", headers=auth(api)).json()
    assert check["status"] == "done" and check["light"] == "red"
    with api["factory"]() as db:
        stored = db.get(Check, status["check_id"])
        assert stored is not None
        package = db.get(Package, stored.package_id)
        assert package is not None and package.title == f"Закупка № {N44} (44-ФЗ)"
        owner = package.owner_id
    me = api["client"].get("/api/auth/me", headers=auth(api)).json()
    assert str(owner) == me["id"]  # пакет — того, кто загрузил: другой специалист его не увидит
    # номер задачи — только для того, кто её запустил
    assert (
        api["client"].get(f"/api/checks/eis/{task_id}", headers=auth(api, "admin")).status_code
        == 404
    )
    assert (
        api["client"].get(f"/api/checks/eis/{task_id.split('.')[0]}", headers=auth(api)).status_code
        == 404
    )


def test_eis_error_is_shown_not_crashed(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(number: str, *, max_files: int, max_bytes: int) -> EisPackage:
        raise EisError("ЕИС сейчас недоступна")

    monkeypatch.setattr(eis_task, "fetch_package", down)
    task_id = start(api, N44).json()["task_id"]
    status = api["client"].get(f"/api/checks/eis/{task_id}", headers=auth(api)).json()
    assert status == {
        "status": "failed",
        "check_id": None,
        "package_id": None,
        "error": "ЕИС сейчас недоступна",
    }


def test_bad_number_limit_and_switch(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(eis_task, "fetch_package", fake_fetch(api, seen))
    for bad in ("123", "https://zakupki.gov.ru/", "0362100026126000023; rm -rf /", "x" * 41):
        response = start(api, bad)
        assert response.status_code in (400, 422), bad
    assert seen == []  # до ЕИС неверный номер не доходит
    monkeypatch.setattr(get_settings(), "max_active_checks_per_user", 1)
    with api["factory"]() as db:  # одна незавершённая проверка специалиста уже есть
        from svetofor.db.models import User
        from svetofor.pipeline.ingest import IngestConfig, create_check
        from svetofor.rules.store import latest_ruleset_version

        user = db.query(User).filter_by(email="specialist@demo.ru").one()
        upload = api["tmp"] / "queued.zip"
        upload.write_bytes(build_upload(api["tmp"]).read_bytes())
        create_check(
            db,
            upload=upload,
            original_name="queued.zip",
            config=IngestConfig(packages_dir=get_settings().packages_dir),
            ruleset_version_id=latest_ruleset_version(db, "44fz-supplier-base").id,  # type: ignore[union-attr]
            owner_id=user.id,
        )
        db.commit()
    assert start(api, N44).status_code == 429
    monkeypatch.setattr(get_settings(), "eis_fetch", False)
    assert start(api, N44, "methodologist").status_code == 404
    assert seen == []
