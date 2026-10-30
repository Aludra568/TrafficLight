"""Поштучная проверка пунктов сквозь конвейер.

Пакеты — из набора «граница GREEN». Модель в первом проходе «слепая» (как в
test_green_boundary.py: риск не видит, подтверждает нормальные пункты), а на отдельном пункте
видит риск — это и есть гипотеза варианта S, которую на настоящей модели замеряет
`make eval-boundary`. Здесь проверяется архитектура: что делает система с её ответами.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import FindingStatus, Light, RuleOutcome
from svetofor.db.models import Check, Finding, RuleResult
from svetofor.eval.boundary import Case, load_cases
from svetofor.llm.backend import ChatReply, ChatRequest
from svetofor.pipeline.sweep import sweep_rules
from svetofor.rules.results import BASIS_EVIDENCE
from tests.integration.test_analysis import deps
from tests.integration.test_green_boundary import BlindModel, _archive
from tests.integration.test_redteam_pipeline import _run

pytestmark = pytest.mark.pg

CASES = {c.id: c for c in load_cases()}
_UNIT = re.compile(r"^\[(U\d+)\][^\n]*\n<<<ДОКУМЕНТ\n(.*?)\nДОКУМЕНТ>>>", re.M | re.S)
_F1 = re.compile(r"^\[F1\][^\n]*\n<<<ДОКУМЕНТ\n(.*?)\nДОКУМЕНТ>>>", re.M | re.S)


def _flat(text: str) -> str:
    return " ".join(text.split())


class UnitSeeingModel(BlindModel):
    """Первый проход — слепой; на отдельном пункте со вставкой случая видит нарушение и
    подтверждает его на повторном вопросе. `omit` — пункты с этим текстом не возвращает."""

    def __init__(self, case: Case, omit: str | None = None, confirm: bool = True) -> None:
        super().__init__(case.rule, "ok")
        self.markers = [_flat(t)[:40] for t in case.inserted] if case.present else []
        self.omit = omit
        self.confirm = confirm
        self.sweep_calls = 0

    def _risky(self, text: str) -> bool:
        return any(m in _flat(text) for m in self.markers)

    def complete(self, request: ChatRequest) -> ChatReply:
        user = request.messages[1].content
        if "Проверь каждый пункт отдельно" in user:
            self.sweep_calls += 1
            units = [
                {
                    "unit_id": alias,
                    "rule_ids": [self.rule] if self._risky(text) else [],
                    "reasoning": "",
                    "verdict": "violation" if self._risky(text) else "none",
                }
                for alias, text in _UNIT.findall(user)
                if not (self.omit and self.omit in _flat(text))
            ]
            return self._reply({"units": units})
        piece = _F1.search(user)
        if "Проверь фрагменты" in user and piece and self._risky(piece.group(1)):
            codes = re.findall(r"^Правило ([A-Z]{2,5}-\d{2,3}):", user, re.M)
            verdict = "violation" if self.confirm else "not_found"
            results = [
                {"rule_id": c, "chunk_id": "F1", "quote": piece.group(1), "verdict": verdict}
                for c in codes
            ]
            return self._reply({"results": results})
        return super().complete(request)

    @staticmethod
    def _reply(content: dict[str, Any]) -> ChatReply:
        return ChatReply(json.dumps(content, ensure_ascii=False), 10, 10, "stop")


def run_case(
    factory: sessionmaker[Session], tmp: Path, case: Case, model: UnitSeeingModel, sweep: bool
) -> tuple[Any, Light | None, dict[str, RuleResult], dict[str, Any]]:
    folder = tmp / f"{case.id}-{sweep}"
    folder.mkdir()
    check_id = _run(factory, folder, _archive(case, folder), deps(model), sweep=sweep)  # type: ignore[arg-type]
    with factory() as db:
        check = db.get(Check, check_id)
        assert check is not None
        results = {
            r.rule_code: r
            for r in db.scalars(select(RuleResult).where(RuleResult.check_id == check_id))
        }
        db.expunge_all()
        return check_id, check.light, results, check.counters


@pytest.mark.parametrize("case_id", ["B01", "B13", "B21", "B29", "B35"])
def test_risk_missed_by_first_pass_is_found_unit_by_unit(
    session_factory: sessionmaker[Session], tmp_path: Path, case_id: str
) -> None:
    """Риск, который первый проход не увидел (без поштучной проверки — ложный GREEN), найден
    в отдельном пункте и подтверждён вопросом правила: пакет красный, цитата — из документа."""
    case = CASES[case_id]
    model = UnitSeeingModel(case)
    _, before, _, _ = run_case(session_factory, tmp_path, case, model, sweep=False)
    assert before is Light.GREEN  # граница: без поштучной проверки — ложный зелёный
    check_id, light, results, counters = run_case(
        session_factory, tmp_path, case, UnitSeeingModel(case), sweep=True
    )
    assert light is Light.RED, results[case.rule].reason
    assert results[case.rule].outcome is RuleOutcome.VIOLATION
    assert case.rule in counters["sweep"]["violations"]
    with session_factory() as db:
        finding = db.scalars(
            select(Finding).where(
                Finding.check_id == check_id,
                Finding.rule_code == case.rule,
                Finding.status == FindingStatus.SHOWN,
            )
        ).one()
        assert _flat(case.inserted[-1])[:30] in _flat(finding.quote or "")
        assert (finding.related or {}).get("sweep") is True


@pytest.mark.parametrize("case_id", ["C01", "C02", "C05", "C13"])
def test_clean_package_stays_green_with_proven_coverage(
    session_factory: sessionmaker[Session], tmp_path: Path, case_id: str
) -> None:
    case = CASES[case_id]
    _, light, results, counters = run_case(
        session_factory, tmp_path, case, UnitSeeingModel(case), sweep=True
    )
    assert light is Light.GREEN
    assert counters["sweep"]["units"] > 10 and counters["sweep"]["unknown"] == []
    trm = results["TRM-01"].details or {}
    assert trm["basis"] == BASIS_EVIDENCE and trm["sweep"]["missing"] == 0


def test_units_without_answer_make_package_gray(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Модель дважды не вернула ответ по пункту 7.2 — «в порядке» не доказано, зелёного нет."""
    case = CASES["C02"]
    model = UnitSeeingModel(case, omit="Решение заказчика об одностороннем отказе")
    _, light, results, counters = run_case(session_factory, tmp_path, case, model, sweep=True)
    assert light is Light.GRAY
    assert (results["TRM-02"].details or {}).get("blocked") == "sweep_incomplete"
    assert "TRM-02" in counters["critical_unchecked"]
    assert model.sweep_calls >= 2  # пропущенный номер спрошен повторно


def test_disputed_unit_is_gray_not_red(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Поштучная проверка отметила пункт, вопрос правила не подтвердил — решает человек."""
    case = CASES["B13"]
    _, light, results, _ = run_case(
        session_factory, tmp_path, case, UnitSeeingModel(case, confirm=False), sweep=True
    )
    assert light is Light.GRAY
    result = results[case.rule]
    assert (result.details or {}).get("blocked") == "sweep_disputed"
    assert "За каждые сутки задержки поставки" in (result.reason or "")


def test_red_package_is_not_swept_again(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    case = CASES["B13"]
    check_id, light, _, _ = run_case(
        session_factory, tmp_path, case, UnitSeeingModel(case), sweep=True
    )
    assert light is Light.RED
    model = UnitSeeingModel(case)
    assert sweep_rules(session_factory, check_id, deps(model)) == {"skipped": "red"}  # type: ignore[arg-type]
    assert model.sweep_calls == 0
