"""Хаос-тест обработки ошибок: настоящий граф задач Celery, модель ломается всеми способами.

Модель стенда — «честная» (`HonestModel`), обёрнутая в генератор сбоев: обрыв связи, отказ,
неожиданное исключение клиента, мусор вместо JSON, оборванный ответ, чужая схема, пустота.
Инварианты, которые не должен нарушить ни один сбой:
- проверка завершается (не остаётся «в работе»), у каждого правила есть исход;
- зелёный — только если сбоев не было: «не проверено» не превращается в «рисков нет»;
- отчёт, выгрузка в Word и запрос на разъяснение отвечают, а не падают 500.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from svetofor.db.enums import CheckStatus, Light
from svetofor.db.models import Check
from svetofor.llm.backend import ChatReply, ChatRequest
from svetofor.llm.errors import LLMRequestRejectedError, LLMUnavailableError
from svetofor.tasks import checks
from tests.integration.conftest import auth
from tests.integration.test_redteam_audit2 import FULL_OK, full_files
from tests.integration.test_redteam_green import HonestModel
from tests.integration.test_redteam_pipeline import _package
from tests.support.rulesets import RULE_COUNT

pytestmark = pytest.mark.pg

FAILURES = (
    "unavailable",
    "rejected",
    "crash",
    "garbage",
    "truncated",
    "wrong_schema",
    "empty",
)
TERMINAL = {CheckStatus.DONE.value, CheckStatus.DONE_WITH_WARNINGS.value, CheckStatus.FAILED.value}


class ChaosModel:
    def __init__(self, inner: HonestModel, rate: float, modes: tuple[str, ...], seed: int) -> None:
        self.inner, self.rate, self.modes = inner, rate, modes
        self.random = random.Random(seed)  # noqa: S311 — воспроизводимые сбои, не криптография
        self.injected: list[str] = []

    def complete(self, request: ChatRequest) -> ChatReply:
        if self.random.random() >= self.rate:
            return self.inner.complete(request)
        mode = self.random.choice(self.modes)
        self.injected.append(mode)
        if mode == "unavailable":
            raise LLMUnavailableError("Нет связи с моделью")
        if mode == "rejected":
            raise LLMRequestRejectedError("Модель отклонила запрос")
        if mode == "crash":  # ошибка внутри клиента, которую никто не ждёт
            raise RuntimeError("неожиданный сбой клиента модели")
        content, finish = {
            "garbage": ("Извините, я не могу помочь с этим запросом.", "stop"),
            "truncated": ('{"results": [{"rule_id": "PAY-0', "length"),
            "wrong_schema": ('{"answer": "всё хорошо", "risks": 0}', "stop"),
            "empty": ("", "stop"),
        }[mode]
        return ChatReply(content, 100, 10, finish)


def _check(api: dict[str, Any], model: ChaosModel) -> tuple[dict[str, Any], TestClient]:
    gateway = checks.services().analysis.gateway
    assert gateway is not None
    gateway._backend = model  # подмена модели стенда
    archive = _package(api["tmp"], full_files())
    client = TestClient(api["client"].app, raise_server_exceptions=False)
    with archive.open("rb") as stream:
        created = client.post(
            "/api/checks",
            headers=auth(api),
            files={"file": ("Пакет.zip", stream, "application/zip")},
        )
    assert created.status_code == 202, created.text
    status = client.get(f"/api/checks/{created.json()['check_id']}", headers=auth(api)).json()
    return status, client


def _assert_invariants(api: dict[str, Any], status: dict[str, Any], client: TestClient) -> None:
    check = status["id"]
    assert status["status"] in TERMINAL, status
    report = client.get(f"/api/checks/{check}/report", headers=auth(api))
    assert report.status_code == 200, report.text
    if status["status"] != CheckStatus.FAILED.value:
        body = report.json()  # у каждого правила есть исход; правила другого закона — отдельно
        assert len(body["rules"]) + body["rules_summary"]["other_law"] == RULE_COUNT
    word = client.post(f"/api/checks/{check}/export", headers=auth(api))
    assert word.status_code == 200, word.text
    draft = client.post(f"/api/checks/{check}/clarification", headers=auth(api), json={})
    assert draft.status_code in (200, 409), draft.text  # 409 — спрашивать не о чем


def test_control_without_failures_is_green(api: dict[str, Any]) -> None:
    model = ChaosModel(HonestModel(oks=FULL_OK), rate=0.0, modes=FAILURES, seed=0)
    status, client = _check(api, model)
    assert status["light"] == Light.GREEN.value, status
    _assert_invariants(api, status, client)


@pytest.mark.parametrize("mode", FAILURES)
def test_model_always_failing_never_green(api: dict[str, Any], mode: str) -> None:
    model = ChaosModel(HonestModel(oks=FULL_OK), rate=1.0, modes=(mode,), seed=0)
    status, client = _check(api, model)
    assert model.injected, "сбой не случился — тест ничего не проверил"
    _assert_invariants(api, status, client)
    assert status["light"] != Light.GREEN.value, status
    assert status["status"] != CheckStatus.DONE.value  # сбой виден: «с предупреждениями»


@pytest.mark.parametrize("seed", range(6))
def test_random_failures_keep_invariants(api: dict[str, Any], seed: int) -> None:
    model = ChaosModel(HonestModel(oks=FULL_OK), rate=0.35, modes=FAILURES, seed=seed)
    status, client = _check(api, model)
    _assert_invariants(api, status, client)
    if status["light"] == Light.GREEN.value:
        # зелёный запрещает «не проверено» из-за сбоя у правил,
        # способных дать «критично»; сбой «жёлтого» правила — «с предупреждениями», не серый
        with api["factory"]() as db:
            stored = db.get(Check, uuid.UUID(status["id"]))
            assert stored is not None
            assert not stored.counters.get("critical_unchecked"), model.injected
            if stored.counters.get("rules_failed"):
                assert stored.status is CheckStatus.DONE_WITH_WARNINGS
