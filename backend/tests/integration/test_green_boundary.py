"""Граница GREEN: архитектура при модели, которая риск не видит.

Набор `eval/boundary/cases.yaml`: 50 рисков словами, которых нет ни в грамматиках, ни в тревожных
формулировках, и 15 законных формулировок. Модель-заглушка «слепая» — так выглядит промах
настоящей модели: по смысловым правилам она отвечает «в порядке» с цитатой нормального пункта
(`ok`) или «не найдено» (`not_found`), нарушений не находит. Что остаётся — детерминированная
страховка архитектуры: грамматики, нечитаемые сроки, тревожные формулировки, полнота, пробелы.

Тест закрепляет то, что должно держаться всегда: законные формулировки остаются зелёными (нет
ложных тревог детерминированного слоя), а риски, которые страховка ловит, — не зелёные. Сколько
рисков проходит зелёными при слепой модели, — это и есть измеряемая граница; её числа пишутся в
отчёт (`BOUNDARY_REPORT=путь.json`), а не утверждаются тестом.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import Light
from svetofor.db.models import Check, RuleResult
from svetofor.eval.boundary import NORMAL_QUOTES, Case, load_cases, package_documents
from svetofor.eval.synthetic import write_pdf
from svetofor.llm.backend import ChatReply, ChatRequest
from tests.integration.test_analysis import deps
from tests.integration.test_redteam_pipeline import _run

pytestmark = pytest.mark.pg

CASES = load_cases()
VALUE_RULES = {"PAY-01", "ACC-01", "TERM-01", "SEC-01"}
RESULTS: dict[str, dict[str, Any]] = {}


class BlindModel:
    """Не видит риска по правилу случая: отвечает по нему «в порядке» с цитатой нормального
    пункта или «не найдено»; по остальным правилам подтверждает нормальные пункты."""

    def __init__(self, rule: str, answer: str) -> None:
        self.rule = rule
        self.answer = answer

    def complete(self, request: ChatRequest) -> ChatReply:
        user = request.messages[1].content
        content: dict[str, Any]
        if "значения полей карточки закупки" in user:
            content = {"facts": []}
        elif "Проверь фрагменты" in user:
            codes = re.findall(r"^Правило ([A-Z]{2,5}-\d{2,3}):", user, flags=re.MULTILINE)
            content = {"results": [self._verdict(code) for code in codes]}
        else:
            content = {"doc_type": "other", "confidence": 0.1}
        body = json.dumps(content, ensure_ascii=False)
        return ChatReply(body, tokens_in=10, tokens_out=10, finish_reason="stop")

    def _verdict(self, code: str) -> dict[str, Any]:
        quote = NORMAL_QUOTES.get(code)
        if (code != self.rule or self.answer == "ok") and quote:
            return {"rule_id": code, "verdict": "ok", "chunk_id": "F1", "quote": quote}
        return {"rule_id": code, "verdict": "not_found", "chunk_id": None, "quote": None}


def _archive(case: Case, folder: Path) -> Path:
    archive = folder / f"{case.id}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, blocks in package_documents(case).items():
            pdf = write_pdf(blocks, folder / name.replace(".docx", ".pdf"))
            zf.write(pdf, pdf.name)
    return archive


def run_case(factory: sessionmaker[Session], tmp: Path, case: Case, answer: str) -> dict[str, Any]:
    folder = tmp / f"{case.id}-{answer}"
    folder.mkdir()
    check_id = _run(factory, folder, _archive(case, folder), deps(BlindModel(case.rule, answer)))  # type: ignore[arg-type]
    with factory() as db:
        check = db.get(Check, check_id)
        assert check is not None
        result = db.scalars(
            select(RuleResult).where(
                RuleResult.check_id == check_id, RuleResult.rule_code == case.rule
            )
        ).one()
        details = result.details or {}
        return {
            "light": check.light.value if check.light else None,
            "outcome": result.outcome.value,
            "basis": details.get("basis"),
            "blocked": details.get("blocked") or details.get("error"),
            "critical": check.counters.get("critical_unchecked", []),
        }


# второй вариант слепой модели — для замера границы (BOUNDARY_FULL=1), по умолчанию — один
ANSWERS = ["ok", "not_found"] if os.environ.get("BOUNDARY_FULL") else ["ok"]


@pytest.mark.parametrize("answer", ANSWERS)
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_boundary_case(
    session_factory: sessionmaker[Session], tmp_path: Path, case: Case, answer: str
) -> None:
    outcome = run_case(session_factory, tmp_path, case, answer)
    RESULTS[f"{case.id}/{answer}"] = {"rule": case.rule, "present": case.present, **outcome}
    if not case.present and answer == "ok":
        # законная формулировка при модели, подтвердившей нормальные пункты, — зелёный
        assert outcome["light"] == Light.GREEN.value, outcome
    if case.present and case.rule in VALUE_RULES and outcome["blocked"]:
        # страховка значений: нечитаемый или неразобранный срок — «не в порядке»
        assert outcome["light"] != Light.GREEN.value, outcome


@pytest.fixture(scope="module", autouse=True)
def _report() -> Any:
    yield
    target = os.environ.get("BOUNDARY_REPORT")
    if target:
        Path(target).write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), "utf-8")
