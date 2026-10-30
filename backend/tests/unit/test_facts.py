"""Карточка закупки: проверка каждого факта и сборка карточки из нескольких документов."""

from __future__ import annotations

import json
from typing import Any

import pytest

from svetofor.core.config import LLMMode
from svetofor.db.enums import DocType
from svetofor.llm.cache import MemoryCache
from svetofor.llm.errors import LLMUnavailableError
from svetofor.llm.gateway import LLMGateway
from svetofor.rules.facts import (
    DocumentFacts,
    FactExtractor,
    SourceChunk,
    coerce_value,
    facts_for,
    fits_whole,
    merge_card,
    numbers_in,
)
from svetofor.rules.facts_catalog import FACTS, display_value
from svetofor.rules.verify import QuoteVerifier
from tests.support.fake_llm import ScriptedBackend, Truncated

CHUNKS = [
    SourceChunk(
        "c-price",
        "doc-1",
        "Проект контракта › 2. Цена",
        1,
        1,
        "2.1. Цена Контракта составляет 1 250 000,00 рублей. Аванс не предусмотрен.",
        100,
        20,
    ),
    SourceChunk(
        "c-pay",
        "doc-1",
        "Проект контракта › 3. Оплата",
        2,
        2,
        "3.1. Оплата производится в течение 7 рабочих дней с даты подписания документа о приёмке.",
        400,
        25,
    ),
    SourceChunk(
        "c-term",
        "doc-1",
        "Проект контракта › 4. Поставка",
        2,
        3,
        "4.1. Поставка осуществляется в течение 10 (десяти) календарных дней с даты заключения.",
        700,
        25,
    ),
]


def fact(
    key: str, value: Any, quote: str | None, chunk: str | None = "F1", **kw: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "quote": quote,
        "chunk_id": chunk,
        "raw": kw.pop("raw", None),
        **kw,
    }


def extractor(*facts: dict[str, Any]) -> tuple[FactExtractor, ScriptedBackend]:
    backend = ScriptedBackend(json.dumps({"facts": list(facts)}, ensure_ascii=False))
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    return FactExtractor(gateway, QuoteVerifier()), backend


def run(*facts: dict[str, Any], doc_type: DocType = DocType.CONTRACT) -> DocumentFacts:
    fx, _ = extractor(*facts)
    return fx.extract(
        document_id="doc-1", doc_type=doc_type, doc_label="Проект контракта", chunks=CHUNKS
    )


def test_verified_facts_with_document_offsets() -> None:
    result = run(
        fact(
            "payment_term_working_days",
            7,
            "в течение 7 рабочих дней с даты подписания",
            "F2",
            unit="working",
            raw="7 рабочих дней",
        ),
        fact("advance_percent", 0, "Аванс не предусмотрен", "F1"),
        fact(
            "delivery_term_days",
            10,
            "в течение 10 (десяти) календарных дней",
            "F3",
            unit="календарных",
        ),
    )
    assert result.error is None and result.rejected == []
    pay = result.facts["payment_term_working_days"]
    assert pay.value == 7.0 and pay.unit == "working" and pay.raw == "7 рабочих дней"
    chunk = CHUNKS[1]
    assert pay.char_start == chunk.char_start + chunk.text.index("в течение 7")
    assert pay.quote == "в течение 7 рабочих дней с даты подписания"
    assert result.facts["advance_percent"].value == 0.0
    assert result.facts["delivery_term_days"].unit == "calendar"
    assert result.model == "m" and result.prompt_version == "facts.v2+system.v1"


def test_quote_from_wrong_chunk_is_still_found() -> None:
    result = run(fact("payment_term_working_days", 7, "в течение 7 рабочих дней", "F1"))
    assert result.facts["payment_term_working_days"].chunk_id == "c-pay"


def test_invented_quote_rejected() -> None:
    result = run(
        fact("payment_term_working_days", 30, "оплата в течение 30 дней после поставки", "F2")
    )
    assert result.facts == {}
    assert [(r.key, r.reason) for r in result.rejected] == [
        ("payment_term_working_days", "цитата не найдена в документе")
    ]


def test_value_must_match_quote() -> None:
    result = run(
        fact("payment_term_working_days", 30, "в течение 7 рабочих дней с даты подписания", "F2")
    )
    assert result.rejected[0].reason == "значение не совпадает с цитатой"


def test_wrong_type_rejected() -> None:
    result = run(fact("advance_percent", True, "Аванс не предусмотрен"))
    assert result.rejected[0].reason == "значение не того типа"


def test_not_found_and_foreign_keys_ignored() -> None:
    result = run(
        fact("warranty_months", None, None, None),
        fact("bid_security_percent", 1, "Цена Контракта"),  # не для контракта
        fact("unknown_key", 5, "Цена Контракта"),
    )
    assert result.facts == {} and result.rejected == []


def test_first_answer_per_key_wins() -> None:
    result = run(
        fact("advance_percent", 0, "Аванс не предусмотрен"),
        fact("advance_percent", 30, "Аванс 30 процентов"),
    )
    assert result.facts["advance_percent"].value == 0.0 and result.rejected == []


def test_model_failure_is_reported_not_invented() -> None:
    backend = ScriptedBackend(LLMUnavailableError("Нет связи с моделью"))
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    result = FactExtractor(gateway, QuoteVerifier()).extract(
        document_id="doc-1", doc_type=DocType.CONTRACT, doc_label="Контракт", chunks=CHUNKS
    )
    assert result.facts == {} and result.error == "Нет связи с моделью"


def test_truncated_answer_splits_fields_in_half() -> None:
    answer = json.dumps(
        {
            "facts": [
                fact("payment_term_working_days", 7, "в течение 7 рабочих дней", "F2"),
                fact("advance_percent", 0, "Аванс не предусмотрен", "F1"),
            ]
        },
        ensure_ascii=False,
    )
    cut = Truncated('{"facts": [{"key": "pay')  # оборвался и с вдвое большим лимитом (шлюз)
    backend = ScriptedBackend(cut, cut, answer, answer)
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    result = FactExtractor(gateway, QuoteVerifier()).extract(
        document_id="doc-1", doc_type=DocType.CONTRACT, doc_label="Контракт", chunks=CHUNKS
    )
    assert len(backend.requests) == 4 and result.error is None
    # каждая половина берёт только свои поля, вместе — все
    assert set(result.facts) == {"payment_term_working_days", "advance_percent"}


def test_context_too_long_fewer_chunks() -> None:
    from dataclasses import replace

    from svetofor.llm.errors import LLMContextTooLongError

    answer = json.dumps(
        {"facts": [fact("payment_term_working_days", 7, "в течение 7 рабочих дней", "F1")]},
        ensure_ascii=False,
    )
    backend = ScriptedBackend(LLMContextTooLongError("длиннее контекста"), answer)
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    chunks = [replace(c, token_count=1000) for c in CHUNKS]
    result = FactExtractor(gateway, QuoteVerifier()).extract(
        document_id="doc-1", doc_type=DocType.CONTRACT, doc_label="Контракт", chunks=chunks
    )
    assert len(backend.requests) == 2 and result.error is None
    assert result.facts["payment_term_working_days"].value == 7
    assert backend.requests[1].messages[1].content.count("<<<ДОКУМЕНТ") < 3  # текста меньше


def test_document_type_without_facts_skips_model() -> None:
    fx, backend = extractor()
    result = fx.extract(document_id="d", doc_type=DocType.NMCK, doc_label="НМЦК", chunks=CHUNKS)
    assert result.facts == {} and backend.requests == []


def test_prompt_mentions_only_relevant_facts() -> None:
    fx, backend = extractor()
    fx.extract(document_id="d", doc_type=DocType.CONTRACT, doc_label="Контракт", chunks=CHUNKS)
    user = backend.requests[0].messages[1].content
    assert "payment_term_working_days" in user and "bid_security_percent" not in user
    assert "[F2] Проект контракта › 3. Оплата (стр. 2)" in user


# ─── Значения ───


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("nmck_rub", 1250000, 1250000.0),
        ("nmck_rub", "1 250 000,00", 1250000.0),
        ("advance_percent", "0,5", 0.5),
        ("advance_percent", -5, None),
        ("advance_percent", True, None),
        ("advance_percent", "10 или 20", None),
        ("smp_only", "да", True),
        ("smp_only", False, False),
        ("smp_only", "возможно", None),
        ("national_regime", "запрет", "ban"),
        ("national_regime", "Restriction", "restriction"),
        ("national_regime", "квота", None),
        ("subject", "  Поставка   бумаги ", "Поставка бумаги"),
        ("subject", "   ", None),
    ],
)
def test_coerce_value(key: str, raw: object, expected: object) -> None:
    assert coerce_value(FACTS[key], raw) == expected


def test_numbers_in() -> None:
    assert numbers_in("1 250 000,00 руб. и 0,5 % и 1/300") == [1250000.0, 0.5, 1.0, 300.0]


def test_procurement_number_must_match_digit_by_digit() -> None:
    chunks = [
        SourceChunk(
            "c",
            "n",
            "Извещение",
            1,
            1,
            "Извещение о проведении электронного аукциона № 0373100012326000123",
            0,
        )
    ]
    for value, ok in (("0373100012326000123", True), ("0373100012326000999", False)):
        backend = ScriptedBackend(
            json.dumps(
                {
                    "facts": [
                        fact(
                            "procurement_number",
                            value,
                            "электронного аукциона № 0373100012326000123",
                        )
                    ]
                }
            )
        )
        gateway = LLMGateway(
            mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
        )
        result = FactExtractor(gateway, QuoteVerifier()).extract(
            document_id="n", doc_type=DocType.NOTICE, doc_label="Извещение", chunks=chunks
        )
        assert ("procurement_number" in result.facts) is ok


# ─── Карточка ───


def test_merge_card_by_source_priority() -> None:
    contract = run(fact("advance_percent", 0, "Аванс не предусмотрен"))
    notice_value = contract.facts["advance_percent"]
    notice = DocumentFacts(
        "doc-2",
        DocType.NOTICE,
        {
            "contract_security_percent": notice_value.__class__(
                **{
                    **notice_value.__dict__,
                    "key": "contract_security_percent",
                    "value": 5.0,
                    "document_id": "doc-2",
                    "doc_type": DocType.NOTICE,
                }
            )
        },
    )
    contract_security = notice_value.__class__(
        **{**notice_value.__dict__, "key": "contract_security_percent", "value": 10.0}
    )
    contract.facts["contract_security_percent"] = contract_security
    card = merge_card([contract, notice])
    assert card["contract_security_percent"].value == 5.0  # извещение важнее контракта
    assert card["advance_percent"].document_id == "doc-1"


def test_catalog_hints_and_sources() -> None:
    for spec in FACTS.values():
        assert spec.hint, f"у факта {spec.key} нет подсказки для модели"
        assert spec.sources
    assert {f.key for f in facts_for(DocType.CONTRACT)} >= {
        "payment_term_working_days",
        "penalty_rate",
    }


def test_fits_whole() -> None:
    assert fits_whole(CHUNKS)
    assert not fits_whole(CHUNKS, budget=50)


@pytest.mark.parametrize(
    ("key", "value", "shown"),
    [
        ("nmck_rub", 1890000.0, "1\u00a0890\u00a0000,00"),  # было «1,89e+06»
        ("nmck_rub", 1250.5, "1\u00a0250,50"),
        ("contract_security_percent", 0.5, "0,5"),
        ("payment_term_days", 30.0, "30"),
        ("payment_term_days", 7, "7"),
    ],
)
def test_numbers_are_shown_in_russian_format(key: str, value: float, shown: str) -> None:
    assert display_value(key, value) == shown
