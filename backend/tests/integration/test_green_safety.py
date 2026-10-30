"""Безопасность зелёного: сквозные проверки внедрённых шагов."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.db.enums import DocType, Light, RuleOutcome
from svetofor.db.models import Document
from svetofor.llm.backend import ChatReply, ChatRequest
from tests.integration.test_redteam_green import (
    CLEAN_CONTRACT,
    HonestModel,
    clean_files,
    pdf,
    run,
)

pytestmark = pytest.mark.pg

PAYMENT_SCHEDULE = """
!! ГРАФИК ОПЛАТЫ
(приложение № 2 к контракту)
1. Оплата производится в течение 45 рабочих дней с даты подписания документа о приемке.
"""


class ConfidentTypes(HonestModel):
    """Честная модель, но тип документа называет уверенно и неверно: «прочее, 0,9»."""

    def __init__(self, doc_type: str = "other", **kw: Any) -> None:
        super().__init__(**kw)
        self.doc_type = doc_type

    def complete(self, request: ChatRequest) -> ChatReply:
        user = request.messages[1].content
        is_type = not any(
            marker in user
            for marker in (
                "значения полей карточки закупки",
                "Проверь фрагменты",
                "Другой аналитик",
            )
        )
        if is_type:
            body = json.dumps({"doc_type": self.doc_type, "confidence": 0.9}, ensure_ascii=False)
            return ChatReply(body, tokens_in=10, tokens_out=10, finish_reason="stop")
        return super().complete(request)


def test_confident_model_cannot_drop_contract_appendix(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Приложение «График оплаты (приложение № 2 к контракту)» с оплатой через 45 рабочих дней.
    Правила видят признак контракта, но не уверены (0,5); модель уверенно отвечает «прочее, 0,9».
    Факт до исправления: тип — «прочее», приложение выпало из правил контракта, PAY-01 «в порядке»,
    пакет ЗЕЛЁНЫЙ. Ожидается: тип по признакам, PAY-01 нарушение, пакет красный; мнение модели
    сохранено в сигналах документа."""
    files = {**clean_files(), "График.pdf": pdf(PAYMENT_SCHEDULE)}
    light, results, _ = run(session_factory, tmp_path, files, ConfidentTypes(), classify=True)
    assert results["PAY-01"].outcome is RuleOutcome.VIOLATION and light is Light.RED
    with session_factory() as db:
        schedule = db.scalars(select(Document).where(Document.filename == "График.pdf")).one()
        assert schedule.doc_type is DocType.CONTRACT
        assert (schedule.signals or {}).get("doc_type_model", {}).get("doc_type") == "other"


def test_model_still_types_document_without_signs(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Контроль: документ без признаков типа модель по-прежнему может отнести к контракту —
    это только добавляет его под проверку (условие из него видят правила контракта)."""
    plain = "!! УСЛОВИЯ\n1. Оплата производится в течение 45 рабочих дней с даты приемки.\n"
    files = {**clean_files(), "Условия.pdf": pdf(plain)}
    model = ConfidentTypes(doc_type="contract")
    light, results, _ = run(session_factory, tmp_path, files, model, classify=True)
    assert results["PAY-01"].outcome is RuleOutcome.VIOLATION and light is Light.RED


# ═════════════════════════ шаг 2: структурная неизвестность ═════════════════════════


def test_unparsed_second_payment_term_is_not_green(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Основной срок оплаты 7 дней (п. 2.2), а второй этап — «по истечении 45 рабочих дней»:
    грамматика такой формы не знает. Факт до исправления: PAY-01 «в порядке» по 7 дням — зелёный.
    Ожидается: PAY-01 — замечание «не найден числом» с цитатой неразобранного места."""
    text = (
        "2.3. Расчёт с Поставщиком за второй этап осуществляется по истечении 45 рабочих дней "
        "с даты приемки."
    )
    files = {**clean_files(), "Проект контракта.pdf": pdf(contract_with(text))}
    light, results, findings = run(session_factory, tmp_path, files, HonestModel())
    assert results["PAY-01"].outcome is RuleOutcome.ABSENT and light is not Light.GREEN
    payment = next(f for f in findings if f.rule_code == "PAY-01")
    assert "45 рабочих дней" in (payment.explanation or "")


def test_missing_mandatory_fines_clause_is_not_green(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Контракт без условия о штрафах (ч. 8 ст. 34 — обязательно), модель честно «не нашла».
    Факт до исправления: «не найдено» = «в порядке по отсутствию» — зелёный. Ожидается: LIA-01 —
    жёлтое «условие о штрафах не найдено — проверьте вручную»."""
    text = CLEAN_CONTRACT.replace(
        "6.3. Штрафы начисляются в соответствии с Правилами, утвержденными постановлением\n"
        "Правительства Российской Федерации № 1042.\n",
        "",
    )
    files = {**clean_files(), "Проект контракта.pdf": pdf(text)}
    light, results, findings = run(session_factory, tmp_path, files, HonestModel())
    assert results["LIA-01"].outcome is RuleOutcome.ABSENT and light is Light.YELLOW
    fines = next(f for f in findings if f.rule_code == "LIA-01")
    assert "обязательно" in fines.message


def contract_with(extra: str) -> str:
    return CLEAN_CONTRACT.replace("**3. СРОКИ ПОСТАВКИ**", f"{extra}\n**3. СРОКИ ПОСТАВКИ**")
