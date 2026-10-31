"""TERM-01: срок поставки, заданный датой, — оценка в днях от окончания подачи заявок."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from svetofor.db.enums import DocType, RuleOutcome, Severity
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import ThresholdRule
from svetofor.rules.results import CheckContext, DocumentView, RuleOutcomeDraft
from svetofor.rules.types import EngineDeps
from svetofor.rules.types.threshold import ThresholdEvaluator
from svetofor.rules.verify import QuoteVerifier

RULES = {
    r.id: r
    for r in load_ruleset_file(
        Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
    ).rules
}
TERM_01 = RULES["TERM-01"]
assert isinstance(TERM_01, ThresholdRule)

DEADLINE = "Дата и время окончания срока подачи заявок 26.10.2026 10:00"


def chunk(chunk_id: str, document_id: str, text: str) -> SourceChunk:
    return SourceChunk(chunk_id, document_id, "Раздел", 1, 1, text, 0)


def text_fact(
    key: str, value: object, source: SourceChunk, quote: str, doc_type: DocType
) -> FactValue:
    start = source.text.index(quote)
    return FactValue(
        key,
        value,  # type: ignore[arg-type]  # в тесте значение подставляется как есть
        None,
        quote,
        quote,
        source.id,
        source.document_id,
        doc_type,
        1,
        start,
        start + len(quote),
        100.0,
    )


def evaluate(
    contract_text: str, *, deadline: bool = True, days: float | None = None
) -> RuleOutcomeDraft:
    contract = chunk("k1", "contract", f"Поставщик обязуется поставить Товар. {contract_text}")
    notice = chunk("n1", "notice", DEADLINE)
    facts = []
    if deadline:
        facts.append(
            DocumentFacts(
                "notice",
                DocType.NOTICE,
                {
                    "application_deadline": text_fact(
                        "application_deadline",
                        "26.10.2026 10:00",
                        notice,
                        "26.10.2026 10:00",
                        DocType.NOTICE,
                    )
                },
            )
        )
    if days is not None:
        quote = f"{int(days)} календарных дней"
        contract = chunk("k1", "contract", f"{contract.text} Срок поставки — {quote}.")
        term = text_fact("delivery_term_days", days, contract, quote, DocType.CONTRACT)
        facts.append(DocumentFacts("contract", DocType.CONTRACT, {"delivery_term_days": term}))
    ctx = CheckContext(
        documents=(
            DocumentView("contract", DocType.CONTRACT, "Проект контракта.docx", (contract,)),
            DocumentView("notice", DocType.NOTICE, "Извещение.txt", (notice,)),
        ),
        facts=tuple(facts),
        law="44-ФЗ",
    )
    return ThresholdEvaluator(EngineDeps(QuoteVerifier())).evaluate([TERM_01], ctx)[0]


def test_date_far_enough_is_ok_with_estimate_note() -> None:
    result = evaluate("Срок поставки товара: до 01.12.2026.")  # 36 − 20 = 16 дн.
    assert result.outcome is RuleOutcome.OK
    assert result.details is not None
    assert "оценка" in result.details["estimate"]
    assert result.details["evidence"][0]["quote"].endswith("до 01.12.2026")


def test_short_estimate_is_yellow_with_explanation() -> None:
    result = evaluate("Срок поставки товара: до 20.11.2026.")  # 25 − 20 = 5 дн.
    assert result.outcome is RuleOutcome.VIOLATION
    (finding,) = result.findings
    assert finding.severity is Severity.YELLOW
    assert finding.explanation is not None and "оценка" in finding.explanation
    assert "≈5" in finding.message


@pytest.mark.parametrize(
    "text",
    [
        "Срок поставки товара: до 15.11.2026.",  # 20 − 20 = 0 дн.: оценке не верим
        "Срок поставки товара: до 01.12.2026. Поставка второй партии — до 15.12.2026.",
    ],
)
def test_doubtful_dates_keep_manual_check(text: str) -> None:
    result = evaluate(text)
    assert result.outcome is RuleOutcome.ABSENT
    assert "не найден числом" in result.findings[0].message


def test_without_deadline_keeps_manual_check() -> None:
    result = evaluate("Срок поставки товара: до 01.12.2026.", deadline=False)
    assert result.outcome is RuleOutcome.ABSENT


def test_days_in_document_win_over_date() -> None:
    result = evaluate("Срок поставки товара: до 20.11.2026.", days=30.0)
    assert result.outcome is RuleOutcome.OK
    assert result.details is not None and "estimate" not in result.details


def test_from_date_only_for_supported_facts() -> None:
    other = RULES["PAY-01"] if isinstance(RULES.get("PAY-01"), ThresholdRule) else None
    base = other or TERM_01
    data = base.model_dump()
    data.update(fact="payment_term_working_days", from_date={"lag_days": 10})
    with pytest.raises(ValidationError, match="from_date"):
        ThresholdRule.model_validate(data)
