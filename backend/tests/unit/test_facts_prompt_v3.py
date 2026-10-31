"""Проверка шаблона facts.v3 и извлечения частичных фактов."""

from __future__ import annotations

import json
from typing import Any

from svetofor.core.config import LLMMode
from svetofor.db.enums import DocType
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.llm.prompting import alias_chunks, render
from svetofor.rules.facts import DocumentFacts, FactExtractor, SourceChunk, facts_for
from svetofor.rules.verify import QuoteVerifier
from tests.support.fake_llm import ScriptedBackend

CHUNKS = [
    SourceChunk(
        "c-notice",
        "doc-notice",
        "Извещение › 1. Общие сведения",
        1,
        1,
        "Начальная (максимальная) цена контракта составляет 1 250 000,00 руб.",
        0,
        20,
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


def test_facts_v3_prompt_rendering() -> None:
    facts = facts_for(DocType.NOTICE)
    raw_chunks = [(c.id, c.section_path, c.page_from, c.page_to, c.text) for c in CHUNKS]
    prompt = render(
        "facts.v3", doc_label="Извещение о закупке", facts=facts, chunks=alias_chunks(raw_chunks)
    )

    assert "только для тех полей" in prompt.user
    assert "Для каждого поля" not in prompt.user
    assert "Поля, которых в документе нет, не включай." in prompt.user
    assert "Если ничего не найдено — пустой список facts." in prompt.user
    assert prompt.version == "facts.v3+system.v1"


def test_facts_v2_prompt_still_works() -> None:
    facts = facts_for(DocType.NOTICE)
    raw_chunks = [(c.id, c.section_path, c.page_from, c.page_to, c.text) for c in CHUNKS]
    prompt = render(
        "facts.v2", doc_label="Извещение о закупке", facts=facts, chunks=alias_chunks(raw_chunks)
    )

    assert "Для каждого поля" in prompt.user
    assert prompt.version == "facts.v2+system.v1"


def test_partial_facts_extracted_without_missing_fields() -> None:
    # Модель возвращает только найденное поле (nmck_rub), все остальные поля извещения опущены
    backend = ScriptedBackend(
        json.dumps(
            {
                "facts": [
                    fact(
                        "nmck_rub",
                        1250000.0,
                        "цена контракта составляет 1 250 000,00 руб.",
                        "F1",
                        raw="1 250 000,00 руб.",
                    )
                ]
            },
            ensure_ascii=False,
        )
    )
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    extractor = FactExtractor(gateway, QuoteVerifier())
    result: DocumentFacts = extractor.extract(
        document_id="doc-notice",
        doc_type=DocType.NOTICE,
        doc_label="Извещение о закупке",
        chunks=CHUNKS,
    )

    assert result.error is None
    assert result.rejected == []
    assert "nmck_rub" in result.facts

    nmck = result.facts["nmck_rub"]
    assert nmck.value == 1250000.0
    assert nmck.quote == "цена контракта составляет 1 250 000,00 руб."

    # Проверяем, что остальные поля извещения отсутствуют в facts и не попали в rejected
    notice_specs = facts_for(DocType.NOTICE)
    assert len(notice_specs) > 1
    for spec in notice_specs:
        if spec.key != "nmck_rub":
            assert spec.key not in result.facts
            assert not any(r.key == spec.key for r in result.rejected)
