"""Движок правил на стартовом наборе: каждый тип правила, честные «не проверено», группы LLM."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from svetofor.core.config import LLMMode
from svetofor.db.enums import DocType, FindingKind, FindingStatus, RuleOutcome, Severity
from svetofor.llm.cache import MemoryCache
from svetofor.llm.errors import LLMUnavailableError
from svetofor.llm.gateway import LLMGateway
from svetofor.rules.engine import RuleEngine
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import AbsentBehavior, LlmCheckRule, Rule
from svetofor.rules.results import CheckContext, DocumentView, RuleOutcomeDraft
from svetofor.rules.types import EngineDeps
from svetofor.rules.types.pattern import sentence_span
from svetofor.rules.verify import QuoteVerifier
from tests.support.fake_llm import ScriptedBackend, Truncated

# Механика движка (группы, бюджеты, второй проход) проверяется без смысловых надстроек набора:
# здесь «в порядке» и «не найдено» по LIA-01 и LIA-03 — условные примеры ответов модели, а не
# смысловая проверка. Тревожные формулировки проверяет tests/unit/test_redteam_audit2.py,
# обязательные темы (отсутствие штрафов и пени — замечание) — tests/unit/test_green_safety.py.
_NEUTRAL = {"red_flags": [], "absent_behavior": AbsentBehavior.IGNORE, "absent_severity": None}
RULES = {
    r.id: (
        r.model_copy(update=_NEUTRAL if r.id in ("LIA-01", "LIA-03") else {"red_flags": []})
        if isinstance(r, LlmCheckRule)
        else r
    )
    for r in load_ruleset_file(
        Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
    ).rules
}

CONTRACT_CHUNKS = (
    SourceChunk(
        "k1",
        "contract",
        "Проект контракта › 3. Оплата",
        2,
        2,
        "3.1. Оплата производится в течение 15 рабочих дней с даты подписания документа "
        "о приёмке. Аванс не предусмотрен.",
        500,
    ),
    SourceChunk(
        "k2",
        "contract",
        "Проект контракта › 4. Поставка",
        2,
        2,
        "4.1. Поставка осуществляется по мере необходимости\nЗаказчика на основании заявок. "
        "Срок поставки — 10 рабочих дней с даты заявки.",
        900,
    ),
    SourceChunk(
        "k3",
        "contract",
        "Проект контракта › 9. Ответственность",
        4,
        4,
        "9.1. За просрочку поставщик уплачивает пеню в размере 1/100 цены контракта "
        "за каждый день просрочки.",
        2000,
    ),
)
SPEC_CHUNKS = (
    SourceChunk(
        "s1",
        "spec",
        "ТЗ › 2. Сроки",
        1,
        1,
        "Срок поставки: 10 календарных дней с даты заключения контракта. Количество: 500 пачек.",
        50,
    ),
)


def fact(
    key: str, value: Any, chunk: SourceChunk, quote: str, doc_type: DocType, unit: str | None = None
) -> FactValue:
    start = chunk.text.index(quote)
    return FactValue(
        key,
        value,
        unit,
        quote,
        quote,
        chunk.id,
        chunk.document_id,
        doc_type,
        chunk.page_from,
        chunk.char_start + start,
        chunk.char_start + start + len(quote),
        100.0,
    )


def context(**kw: Any) -> CheckContext:
    contract_facts = DocumentFacts(
        "contract",
        DocType.CONTRACT,
        {
            "payment_term_working_days": fact(
                "payment_term_working_days",
                15.0,
                CONTRACT_CHUNKS[0],
                "в течение 15 рабочих дней",
                DocType.CONTRACT,
                "working",
            ),
            "advance_percent": fact(
                "advance_percent",
                0.0,
                CONTRACT_CHUNKS[0],
                "Аванс не предусмотрен",
                DocType.CONTRACT,
            ),
            "delivery_term_days": fact(
                "delivery_term_days",
                10.0,
                CONTRACT_CHUNKS[1],
                "10 рабочих дней с даты заявки",
                DocType.CONTRACT,
                "working",
            ),
        },
    )
    spec_facts = DocumentFacts(
        "spec",
        DocType.SPEC,
        {
            "delivery_term_days": fact(
                "delivery_term_days",
                10.0,
                SPEC_CHUNKS[0],
                "10 календарных дней",
                DocType.SPEC,
                "calendar",
            ),
        },
    )
    defaults: dict[str, Any] = {
        "documents": (
            DocumentView("contract", DocType.CONTRACT, "Проект контракта.docx", CONTRACT_CHUNKS),
            DocumentView("spec", DocType.SPEC, "ТЗ.docx", SPEC_CHUNKS),
        ),
        "facts": (contract_facts, spec_facts),
        "law": "44-ФЗ",
    }
    return CheckContext(**{**defaults, **kw})


def engine(
    *replies: str | Exception, retrieved: tuple[SourceChunk, ...] = CONTRACT_CHUNKS
) -> tuple[RuleEngine, ScriptedBackend, list[Any]]:
    backend = ScriptedBackend(*replies)  # type: ignore[arg-type]
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="", backend=backend, cache=MemoryCache()
    )
    calls: list[Any] = []

    def retrieve(doc_types: Any, requests: Any, budget: int | None = None) -> list[SourceChunk]:
        calls.append((list(doc_types), list(requests), budget))
        if budget is None:
            return list(retrieved)
        kept: list[SourceChunk] = []  # как настоящий поиск: первые фрагменты в пределах бюджета
        for chunk in retrieved:
            if kept and sum(c.token_count for c in kept) + chunk.token_count > budget:
                break
            kept.append(chunk)
        return kept

    deps = EngineDeps(verifier=QuoteVerifier(), gateway=gateway, retrieve=retrieve)
    return RuleEngine(deps), backend, calls


def run(
    rules: list[str], ctx: CheckContext | None = None, *replies: str | Exception
) -> dict[str, RuleOutcomeDraft]:
    eng, _, _ = engine(*replies)
    outcomes = eng.run([RULES[r] for r in rules], ctx or context())
    return {o.rule_id: o for o in outcomes}


# ─── threshold ───


def test_threshold_first_matching_condition_sets_color() -> None:
    outcome = run(["PAY-01"])["PAY-01"]
    assert outcome.outcome is RuleOutcome.VIOLATION
    finding = outcome.findings[0]
    assert finding.severity is Severity.YELLOW  # 15 > 7, но не > 30
    assert finding.message.startswith("Оплата через 15 раб. дн.")
    assert finding.evidence[0].quote == "в течение 15 рабочих дней"
    assert finding.evidence[0].section_path == "Проект контракта › 3. Оплата"
    assert finding.evidence[0].char_start == 500 + CONTRACT_CHUNKS[0].text.index("в течение 15")


def test_threshold_ok_and_zero_advance() -> None:
    ctx = context()
    pay = ctx.facts[0].facts["payment_term_working_days"]
    ctx.facts[0].facts["payment_term_working_days"] = replace(pay, value=5.0)
    outcomes = run(["PAY-01", "PAY-02"], ctx)
    assert outcomes["PAY-01"].outcome is RuleOutcome.OK
    assert outcomes["PAY-02"].outcome is RuleOutcome.VIOLATION  # value == 0


def test_threshold_calendar_days_converted() -> None:
    ctx = context()
    pay = ctx.facts[0].facts["payment_term_working_days"]
    ctx.facts[0].facts["payment_term_working_days"] = replace(pay, value=14.0, unit="calendar")
    finding = run(["PAY-01"], ctx)["PAY-01"].findings[0]
    assert "≈10" in finding.message  # 14 календарных ≈ 10 рабочих
    assert finding.explanation is not None and "×5/7" in finding.explanation


def _with_conflict(model_value: float, rules_value: float, rules_quote: str) -> CheckContext:
    ctx = context()
    pay = ctx.facts[0].facts["payment_term_working_days"]
    chunk = CONTRACT_CHUNKS[0]
    start = chunk.char_start + chunk.text.index(rules_quote)
    ctx.facts[0].facts["payment_term_working_days"] = replace(
        pay,
        value=model_value,
        conflict={
            "value": rules_value,
            "unit": "working",
            "quote": rules_quote,
            "chunk_id": chunk.id,
            "char_start": start,
            "char_end": start + len(rules_quote),
            "page": chunk.page_from,
        },
    )
    return ctx


def test_threshold_channels_disagree_worse_value_wins() -> None:
    """Модель: 5 дней (норма), грамматика: 15 (жёлтый) — не «в порядке», а жёлтый с пояснением."""
    ctx = _with_conflict(5.0, 15.0, "в течение 15 рабочих дней")
    outcome = run(["PAY-01"], ctx)["PAY-01"]
    assert outcome.outcome is RuleOutcome.VIOLATION
    finding = outcome.findings[0]
    assert finding.severity is Severity.YELLOW and finding.message.startswith("Оплата через 15")
    assert finding.explanation is not None
    assert "Модель прочитала другое значение — 5" in finding.explanation
    assert "по худшему из двух значений" in finding.explanation
    # оба места в документе — для проверки специалистом
    assert [e.quote for e in finding.evidence] == [
        "в течение 15 рабочих дней",
        "в течение 15 рабочих дней",
    ]
    assert finding.evidence[1].section_path == "Проект контракта › 3. Оплата"


def test_threshold_channels_disagree_model_value_already_worse() -> None:
    ctx = _with_conflict(40.0, 15.0, "в течение 15 рабочих дней")
    finding = run(["PAY-01"], ctx)["PAY-01"].findings[0]
    assert finding.severity is Severity.RED  # 40 > 30
    assert finding.explanation is not None
    assert "Второй способ проверки нашёл другое значение — 15" in finding.explanation


def test_threshold_channels_disagree_both_fine() -> None:
    ctx = _with_conflict(5.0, 6.0, "в течение 15 рабочих дней")
    assert run(["PAY-01"], ctx)["PAY-01"].outcome is RuleOutcome.OK


def test_threshold_missing_fact_is_not_checked() -> None:
    outcome = run(["WAR-01"])["WAR-01"]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED
    assert outcome.reason == "в документах не найдено: гарантийный срок"


def test_mandatory_term_missing_is_finding() -> None:
    """Срок приёмки — обязательное условие: не найден числом — замечание «проверьте вручную»,
    а не «не проверено», с которым пакет мог быть зелёным."""
    outcome = run(["ACC-01"])["ACC-01"]
    assert outcome.outcome is RuleOutcome.ABSENT
    assert "не найден числом" in outcome.findings[0].message


def test_threshold_when_facts_failed() -> None:
    ctx = context(facts=(DocumentFacts("contract", DocType.CONTRACT, error="Нет связи с моделью"),))
    outcome = run(["PAY-01"], ctx)["PAY-01"]
    assert outcome.reason == "карточка закупки не заполнена: Нет связи с моделью"


# ─── pattern ───


def test_pattern_across_line_break() -> None:
    outcome = run(["VOL-01"])["VOL-01"]
    assert outcome.outcome is RuleOutcome.VIOLATION
    evidence = outcome.findings[0].evidence[0]
    assert evidence.quote.startswith("4.1. Поставка осуществляется по мере необходимости")
    assert evidence.quote.endswith("на основании заявок.")
    assert evidence.char_start == 900 and evidence.score == 100.0


def test_pattern_no_match_is_ok() -> None:
    ctx = context(
        documents=(DocumentView("contract", DocType.CONTRACT, "К.docx", CONTRACT_CHUNKS[2:]),)
    )
    assert run(["VOL-01"], ctx)["VOL-01"].outcome is RuleOutcome.OK


@pytest.mark.parametrize(
    ("text", "needle", "expected"),
    [
        (
            "Первое. 4.1. Поставка по мере необходимости. Третье.",
            "по мере",
            "4.1. Поставка по мере необходимости.",
        ),
        (
            "Склад в г. Москва принимает по мере заявок. Далее.",
            "по мере",
            "Склад в г. Москва принимает по мере заявок.",
        ),
        (
            "Пеня 1 000 руб. в день по ст. 34 Закона. Конец.",
            "в день",
            "Пеня 1 000 руб. в день по ст. 34 Закона.",
        ),
        (  # заголовок раздела без точки не прилипает, следующий пункт не захватывается
            "4. ОТВЕТСТВЕННОСТЬ СТОРОН\n4.1. Пеня в размере 1/100\nцены контракта\n4.2. Штраф",
            "в размере",
            "4.1. Пеня в размере 1/100\nцены контракта",
        ),
    ],
)
def test_sentence_span_keeps_numbers_and_abbreviations(
    text: str, needle: str, expected: str
) -> None:
    start = text.index(needle)
    left, right = sentence_span(text, start, start + len(needle))
    assert text[left:right] == expected


def test_sentence_span_long_text_is_clamped() -> None:
    text = "слово " * 200 + "по мере необходимости " + "слово " * 200
    start = text.index("по мере")
    left, right = sentence_span(text, start, start + 10)
    assert right - left < 420 and text[left:right].strip() == text[left:right]


# ─── llm_check ───


def verdicts(*items: dict[str, Any]) -> str:
    return json.dumps({"results": list(items)}, ensure_ascii=False)


def test_llm_group_one_call_with_verified_quote() -> None:
    reply = verdicts(
        {
            "rule_id": "LIA-03",
            "verdict": "violation",
            "chunk_id": "F3",
            "quote": "пеню в размере 1/100 цены контракта за каждый день просрочки",
            "explanation": "Пеня 1/100 больше законной 1/300.",
        },
        {
            "rule_id": "LIA-01",
            "verdict": "not_found",
            "chunk_id": None,
            "quote": None,
            "explanation": "",
        },
        {
            "rule_id": "LIA-02",
            "verdict": "not_found",
            "chunk_id": None,
            "quote": None,
            "explanation": "",
        },
    )
    eng, backend, calls = engine(reply)
    outcomes = {
        o.rule_id: o
        for o in eng.run([RULES["LIA-01"], RULES["LIA-02"], RULES["LIA-03"]], context())
    }
    assert len(backend.requests) == 1  # одна группа — один вызов
    # контракт помещается целиком: модель читает весь текст, поиск не нужен
    assert calls == []
    prompt = backend.requests[0].messages[1].content
    assert all(c.text.split()[0] in prompt for c in CONTRACT_CHUNKS)
    assert prompt.index("<<<ДОКУМЕНТ") < prompt.index("Правило LIA-01")  # документ — первым
    lia3 = outcomes["LIA-03"]
    assert lia3.outcome is RuleOutcome.VIOLATION
    assert (
        lia3.findings[0].evidence[0].quote
        == "пеню в размере 1/100 цены контракта за каждый день просрочки"
    )
    assert lia3.findings[0].explanation == "Пеня 1/100 больше законной 1/300."
    lia1 = outcomes["LIA-01"]  # not_found + ignore → «в порядке» по отсутствию, с местом поиска
    assert lia1.outcome is RuleOutcome.OK and lia1.details is not None
    assert lia1.details["basis"] == "absence"
    assert lia1.details["search_scope"] == "весь текст: «Проект контракта.docx»"
    lia2 = outcomes["LIA-02"]  # not_found + report → «условие не найдено»
    assert lia2.outcome is RuleOutcome.ABSENT and lia2.findings[0].kind is FindingKind.ABSENT
    # модель уже видела весь контракт — второй проход не нужен, но место поиска записано
    assert lia2.findings[0].related == {
        "checked_documents": ["Проект контракта.docx"],
        "search_scope": "весь текст: «Проект контракта.docx»",
    }


LIA02_NOT_FOUND = verdicts({"rule_id": "LIA-02", "verdict": "not_found"})
# контракт длиннее «документа целиком» (12 000 токенов): первый проход — по выдаче поиска;
# второй проход берёт фрагменты с ключевыми словами в пределах бюджета (пункт 9.1 — всегда)
BIG_CHUNKS = tuple(
    replace(c, token_count=t) for c, t in zip(CONTRACT_CHUNKS, (11_000, 11_000, 600), strict=True)
)


def big_context() -> CheckContext:
    return context(
        documents=(
            DocumentView("contract", DocType.CONTRACT, "Проект контракта.docx", BIG_CHUNKS),
            DocumentView("spec", DocType.SPEC, "ТЗ.docx", SPEC_CHUNKS),
        )
    )


def test_absent_confirmed_by_second_pass_over_keyword_fragments() -> None:
    """Поиск дал часть контракта, модель «не нашла» — второй проход по фрагментам с ключевыми
    словами правила; где искали — записано в замечании."""
    eng, backend, _ = engine(LIA02_NOT_FOUND, LIA02_NOT_FOUND, retrieved=BIG_CHUNKS[:1])
    outcome = eng.run([RULES["LIA-02"]], big_context())[0]
    assert len(backend.requests) == 2
    assert "9.1. За просрочку" in backend.requests[1].messages[1].content
    assert outcome.outcome is RuleOutcome.ABSENT
    assert outcome.findings[0].related is not None
    scope = outcome.findings[0].related["search_scope"]
    assert scope.endswith("с ключевыми словами правила") and "из 3 фрагментов" in scope


def test_ignored_absence_also_gets_second_pass() -> None:
    """Правило без «сообщать об отсутствии» (TRM-01) — тоже второй проход."""
    not_found = verdicts({"rule_id": "LIA-01", "verdict": "not_found"})
    eng, backend, _ = engine(not_found, not_found, retrieved=BIG_CHUNKS[:1])
    outcome = eng.run([RULES["LIA-01"]], big_context())[0]
    assert len(backend.requests) == 2
    assert outcome.outcome is RuleOutcome.OK and outcome.details is not None
    assert outcome.details["basis"] == "absence" and outcome.details["second_pass"] is True


def test_second_pass_finds_what_search_missed() -> None:
    found = verdicts(
        {
            "rule_id": "LIA-02",
            "verdict": "violation",
            "chunk_id": "F3",
            "quote": "пеню в размере 1/100 цены контракта за каждый день просрочки",
            "explanation": "Ответственность есть только у поставщика.",
        }
    )
    eng, backend, _ = engine(LIA02_NOT_FOUND, found, retrieved=BIG_CHUNKS[:1])
    outcome = eng.run([RULES["LIA-02"]], big_context())[0]
    assert len(backend.requests) == 2 and outcome.outcome is RuleOutcome.VIOLATION
    assert outcome.findings[0].evidence[0].section_path == "Проект контракта › 9. Ответственность"


def test_second_pass_failure_is_not_checked_not_absent() -> None:
    eng, _, _ = engine(
        LIA02_NOT_FOUND, LLMUnavailableError("Нет связи с моделью"), retrieved=BIG_CHUNKS[:1]
    )
    outcome = eng.run([RULES["LIA-02"]], big_context())[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED
    assert outcome.reason == "не удалось подтвердить отсутствие условия: Нет связи с моделью"


def test_ok_verdict_keeps_verified_quote_and_context() -> None:
    """«Почему зелёный»: у «в порядке» — проверенная цитата; записано, что видела модель."""
    reply = verdicts(
        {
            "rule_id": "LIA-03",
            "chunk_id": "F3",
            "quote": "пеню в размере 1/100 цены контракта",
            "explanation": "x",
            "verdict": "ok",
        }
    )
    outcome = run(["LIA-03"], None, reply)["LIA-03"]
    assert outcome.outcome is RuleOutcome.OK and outcome.details is not None
    assert outcome.details["context"] == [c.id for c in CONTRACT_CHUNKS]
    (evidence,) = outcome.details["evidence"]
    assert evidence["quote"] == "пеню в размере 1/100 цены контракта"
    assert evidence["section_path"] == "Проект контракта › 9. Ответственность"


def test_ok_verdict_with_invented_quote_has_no_evidence() -> None:
    reply = verdicts(
        {
            "rule_id": "LIA-03",
            "chunk_id": "F1",
            "quote": "пеня не начисляется вовсе",
            "verdict": "ok",
        }
    )
    outcome = run(["LIA-03"], None, reply)["LIA-03"]
    assert outcome.outcome is RuleOutcome.OK and outcome.details is not None
    assert "evidence" not in outcome.details  # невыдуманное «основание» не показываем


def test_threshold_ok_shows_fact_quote() -> None:
    ctx = context()
    pay = ctx.facts[0].facts["payment_term_working_days"]
    ctx.facts[0].facts["payment_term_working_days"] = replace(pay, value=5.0)
    outcome = run(["PAY-01"], ctx)["PAY-01"]
    assert outcome.outcome is RuleOutcome.OK and outcome.details is not None
    assert outcome.details["evidence"][0]["quote"] == "в течение 15 рабочих дней"


def test_llm_invented_quote_is_rejected_not_shown() -> None:
    reply = verdicts(
        {
            "rule_id": "LIA-03",
            "verdict": "violation",
            "chunk_id": "F3",
            "quote": "пеня составляет 1/50 ставки за каждый час просрочки",
            "explanation": "x",
        }
    )
    outcome = run(["LIA-03"], None, reply)["LIA-03"]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED
    assert outcome.shown == [] and outcome.rejected[0].status is FindingStatus.REJECTED_UNVERIFIED
    assert outcome.rejected[0].related is not None


def test_llm_quote_from_wrong_document_type_is_rejected() -> None:
    # SUB-01 смотрит только ТЗ: цитата из контракта не должна подтвердить нарушение
    reply = verdicts(
        {
            "rule_id": "SUB-01",
            "verdict": "violation",
            "chunk_id": "F1",
            "quote": "Оплата производится в течение 15 рабочих дней",
            "explanation": "x",
        }
    )
    eng, _, _ = engine(reply, retrieved=CONTRACT_CHUNKS + SPEC_CHUNKS)
    outcome = eng.run([RULES["SUB-01"]], context())[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED and outcome.shown == []


def test_llm_unavailable_marks_not_checked() -> None:
    outcomes = run(["TRM-01", "TRM-02"], None, LLMUnavailableError("Нет связи с моделью"))
    assert {o.outcome for o in outcomes.values()} == {RuleOutcome.NOT_CHECKED}
    assert outcomes["TRM-01"].reason == "Нет связи с моделью"


def test_llm_missing_verdict_for_rule() -> None:
    reply = verdicts(
        {"rule_id": "TRM-01", "verdict": "ok", "chunk_id": "F1", "quote": None, "explanation": ""}
    )
    outcomes = run(["TRM-01", "TRM-02"], None, reply)
    assert outcomes["TRM-01"].outcome is RuleOutcome.OK
    assert outcomes["TRM-02"].reason == "модель не дала ответ по этому правилу"


def test_llm_search_found_nothing_model_reads_whole_document() -> None:
    eng, backend, _ = engine(LIA02_NOT_FOUND, retrieved=())
    outcome = eng.run([RULES["LIA-02"]], context())[0]
    assert outcome.outcome is RuleOutcome.ABSENT and len(backend.requests) == 1


def test_llm_document_without_text_is_not_checked() -> None:
    """В документе нет текста: «условие не найдено» было бы ложным — «не проверено»."""
    empty = DocumentView("contract", DocType.CONTRACT, "Проект контракта.docx", ())
    eng, backend, _ = engine(retrieved=())
    outcome = eng.run([RULES["LIA-02"]], context(documents=(empty,)))[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED and backend.requests == []
    assert "нет распознанного текста" in (outcome.reason or "")
    assert outcome.findings == ()


def test_large_group_split_into_batches() -> None:
    eng, backend, _ = engine(*([verdicts()] * 2))
    eng.deps.max_rules_per_call = 2
    eng.run([RULES["LIA-01"], RULES["LIA-02"], RULES["LIA-03"]], context())
    assert len(backend.requests) == 2


def test_truncated_answer_splits_group_in_half() -> None:
    """Ответ по группе из 3 правил оборвался и с вдвое большим лимитом (повтор — в шлюзе) —
    половины проверяются отдельно, ничего не теряется."""
    first_half = verdicts({"rule_id": "LIA-01", "verdict": "ok", "quote": None})
    second_half = verdicts(
        {"rule_id": "LIA-02", "verdict": "ok"},
        {"rule_id": "LIA-03", "verdict": "ok"},
    )
    cut = Truncated('{"results": [{"rule_id": "LI')
    eng, backend, _ = engine(cut, cut, first_half, second_half)
    outcomes = eng.run([RULES["LIA-01"], RULES["LIA-02"], RULES["LIA-03"]], context())
    assert len(backend.requests) == 4  # группа дважды (повтор шлюза), потом каждая половина
    # у LIA-02 отсутствие условия — само риск: «в порядке» без цитаты не принимается (аудит 2,
    # AR-12); остальные ответы половин дошли
    assert [o.outcome for o in outcomes] == [
        RuleOutcome.OK,
        RuleOutcome.NOT_CHECKED,
        RuleOutcome.OK,
    ]
    assert (outcomes[1].details or {}).get("error") == "unverified_ok"


def test_truncated_single_rule_is_not_checked() -> None:
    eng, _, _ = engine(Truncated('{"results": ['), Truncated('{"results": ['))
    outcome = eng.run([RULES["LIA-03"]], context())[0]
    assert outcome.outcome is RuleOutcome.NOT_CHECKED and "оборвался" in (outcome.reason or "")


def test_explanation_with_invented_numbers_is_dropped() -> None:
    from svetofor.rules.types.llm_check import trusted_explanation

    rule = RULES["LIA-03"]
    quote = "пеню в размере 1/100 цены контракта за каждый день просрочки"
    assert trusted_explanation("Пеня 1/100 выше законной 1/300.", quote, rule) is not None
    # «ст. 34 ч. 7» и «5 процентов» — ни в цитате, ни в правиле: модель досказала
    assert trusted_explanation("Нарушена ч. 7 ст. 34: пеня выше 5 процентов.", quote, rule) is None
    assert trusted_explanation("   ", quote, rule) is None


def test_context_too_long_single_rule_retries_with_less_text() -> None:
    from svetofor.llm.errors import LLMContextTooLongError

    ok = verdicts({"rule_id": "LIA-03", "verdict": "ok"})
    eng, backend, calls = engine(
        LLMContextTooLongError("длиннее контекста"), ok, retrieved=BIG_CHUNKS
    )
    outcome = eng.run([RULES["LIA-03"]], big_context())[0]
    assert outcome.outcome is RuleOutcome.OK and len(backend.requests) == 2
    assert calls[1][2] == 11_300  # вдвое меньше текста
    assert outcome.details is not None and len(outcome.details["context"]) == 1


def test_context_too_long_group_split() -> None:
    """Весь текст не влез → поиск; группа и по поиску не влезла → половины по отдельности."""
    from svetofor.llm.errors import LLMContextTooLongError

    first = verdicts({"rule_id": "LIA-01", "verdict": "ok"})
    second = verdicts({"rule_id": "LIA-03", "verdict": "ok"})
    too_long = LLMContextTooLongError("длиннее контекста")
    eng, backend, _ = engine(too_long, too_long, first, second)
    outcomes = eng.run([RULES["LIA-01"], RULES["LIA-03"]], context())
    assert [o.outcome for o in outcomes] == [RuleOutcome.OK, RuleOutcome.OK]
    assert len(backend.requests) == 4


def test_budgets_follow_context_length() -> None:
    from svetofor.llm.prompting import Budgets

    assert Budgets.for_context(8192) == Budgets(2048, 4096)
    assert Budgets.for_context(128_000) == Budgets(6000, 12_000)


# ─── второе мнение ───

LIA03_VIOLATION = verdicts(
    {
        "rule_id": "LIA-03",
        "chunk_id": "F3",
        "quote": "пеню в размере 1/100 цены контракта за каждый день просрочки",
        "explanation": "Пеня 1/100 выше законной 1/300.",
        "verdict": "violation",
    }
)


def _review(decision: str, reasoning: str = "") -> str:
    return json.dumps(
        {"reviews": [{"item_id": "R1", "reasoning": reasoning, "decision": decision}]},
        ensure_ascii=False,
    )


def _with_review(*replies: str | Exception) -> RuleOutcomeDraft:
    eng, backend, _ = engine(*replies)
    eng.deps.second_opinion = True
    outcome = eng.run([RULES["LIA-03"]], context())[0]
    assert len(backend.requests) == 2
    review_prompt = backend.requests[1].messages[1].content
    assert (
        "Другой аналитик решил" in review_prompt and "Решение R1 — правило LIA-03" in review_prompt
    )
    assert (
        "9.1. За просрочку" in review_prompt
    )  # рецензент видит окружающий текст, а не только цитату
    return outcome


def test_second_opinion_confirms_red_violation() -> None:
    outcome = _with_review(LIA03_VIOLATION, _review("confirmed", "Пеня выше законной."))
    related = outcome.findings[0].related or {}
    assert related["review"] == {"decision": "confirmed", "comment": "Пеня выше законной."}
    assert "needs_review" not in related


def test_second_opinion_disagrees_finding_kept_and_marked() -> None:
    outcome = _with_review(LIA03_VIOLATION, _review("rejected", "Пункт о неустойке заказчика."))
    finding = outcome.findings[0]
    assert outcome.outcome is RuleOutcome.VIOLATION and finding.severity is Severity.RED
    assert (finding.related or {})["needs_review"] is True  # не скрыто и не перекрашено


def test_second_opinion_failure_leaves_finding_as_is() -> None:
    outcome = _with_review(LIA03_VIOLATION, LLMUnavailableError("Нет связи"))
    assert "review" not in (outcome.findings[0].related or {})


def test_second_opinion_off_by_default_in_engine() -> None:
    eng, backend, _ = engine(LIA03_VIOLATION)
    eng.run([RULES["LIA-03"]], context())
    assert len(backend.requests) == 1


# ─── signal ───


def test_hidden_text_signal_quotes_exact_place() -> None:
    from svetofor.rules.results import HiddenText

    chunk = CONTRACT_CHUNKS[2]
    secret = "за каждый день просрочки"
    start = chunk.char_start + chunk.text.index(secret)
    contract = DocumentView(
        "contract",
        DocType.CONTRACT,
        "Проект контракта.docx",
        CONTRACT_CHUNKS,
        (HiddenText(4, start, start + len(secret), secret),),
    )
    outcome = run(["CON-03"], context(documents=(contract,)))["CON-03"]
    assert outcome.outcome is RuleOutcome.VIOLATION
    finding = outcome.findings[0]
    assert finding.evidence[0].quote == secret and finding.evidence[0].char_start == start
    assert secret in finding.message


def test_no_hidden_text_is_ok() -> None:
    assert run(["CON-03"])["CON-03"].outcome is RuleOutcome.OK


# ─── cross_document ───


def test_cross_document_units_differ() -> None:
    outcome = run(["CON-01"])["CON-01"]
    assert outcome.outcome is RuleOutcome.VIOLATION
    finding = outcome.findings[0]
    assert finding.kind is FindingKind.CONTRADICTION
    assert finding.message == (
        "Срок поставки в ТЗ (10 календ. дн.) не совпадает со сроком в проекте контракта "
        "(10 раб. дн.)."
    )
    assert [e.document_id for e in finding.evidence] == ["spec", "contract"]


def test_cross_document_equal_and_missing() -> None:
    ctx = context()
    spec_fact = ctx.facts[1].facts["delivery_term_days"]
    ctx.facts[1].facts["delivery_term_days"] = replace(spec_fact, unit="working")
    outcomes = run(["CON-01", "CON-02"], ctx)
    assert outcomes["CON-01"].outcome is RuleOutcome.OK
    assert outcomes["CON-02"].outcome is RuleOutcome.NOT_CHECKED  # количество нигде не извлечено


# ─── применимость ───


def test_rules_for_other_law_or_missing_documents() -> None:
    ctx = context(law="223-ФЗ")
    outcomes = run(["PAY-01", "TERM-01"], ctx)
    assert outcomes["PAY-01"].reason == "правило для 44-ФЗ, а закупка — 223-ФЗ"
    # TERM-01 написано и для 223-ФЗ — применяется (срок из ТЗ: 10 дней — не меньше 10, риска нет)
    assert outcomes["TERM-01"].outcome is RuleOutcome.OK
    only_spec = context(documents=(DocumentView("spec", DocType.SPEC, "ТЗ.docx", SPEC_CHUNKS),))
    assert run(["PAY-01"], only_spec)["PAY-01"].reason == "в пакете нет документа: проект контракта"


def test_disabled_rules_are_skipped_and_order_kept() -> None:
    disabled: Rule = RULES["PAY-02"].model_copy(update={"enabled": False})
    eng, _, _ = engine()
    outcomes = eng.run([RULES["VOL-01"], disabled, RULES["PAY-01"]], context())
    assert [o.rule_id for o in outcomes] == ["VOL-01", "PAY-01"]


@pytest.mark.parametrize("rule_id", sorted(RULES))
def test_every_starter_rule_gets_exactly_one_outcome(rule_id: str) -> None:
    eng, _, _ = engine(*([verdicts()] * 3))
    outcomes = eng.run([RULES[rule_id]], context())
    assert len(outcomes) == 1 and outcomes[0].rule_id == rule_id
