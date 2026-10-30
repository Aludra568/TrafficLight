"""Безопасность зелёного: модельные проверки внедрённых шагов.

Шаг 1 — модель не выводит документ из-под проверки: ни тип документа, ни закон закупки не
решаются одним её голосом. Шаг 2 — структурная неизвестность: неразобранное значение поля,
отсутствие обязательной темы, неизвестный исход правила «критично» — не зелёный.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest

from svetofor.db.enums import DocType, Light, RuleOutcome, Severity
from svetofor.pipeline.doctype import DocTypeGuess, choose_doc_type
from svetofor.rules.aggregate import (
    critical_unchecked,
    is_failure,
    light_from_severities,
    package_verdict,
)
from svetofor.rules.engine import RuleEngine
from svetofor.rules.fact_rules import extract_rule_facts, unparsed_terms
from svetofor.rules.facts import DocumentFacts, FactValue, SourceChunk, procurement_law
from svetofor.rules.model import AbsentBehavior, LlmCheckRule
from svetofor.rules.results import CheckContext, counts_as_assessed
from svetofor.rules.types import EngineDeps
from svetofor.rules.verify import QuoteVerifier
from tests.unit.test_redteam import RULES, chunk, view

# ═════════════════════════ шаг 1: тип документа ═════════════════════════

RULES_CONTRACT = DocTypeGuess(DocType.CONTRACT, 0.5, "по признакам: приложение к контракту")
NO_SIGNS = DocTypeGuess(DocType.OTHER, 0.0, "нет характерных признаков")


def test_model_cannot_move_document_with_signs_to_other() -> None:
    """«График оплаты (приложение № 2 к контракту)»: модель уверенно говорит «прочее» — тип
    остаётся по признакам, иначе приложение с оплатой через 45 дней выпадало из проверки."""
    guess, by_model = choose_doc_type(RULES_CONTRACT, DocTypeGuess(DocType.OTHER, 0.9, "модель"))
    assert guess.doc_type is DocType.CONTRACT and not by_model


def test_model_cannot_switch_type_against_signs() -> None:
    """Другой конкретный тип по модели при признаках контракта — тоже не её решение."""
    guess, by_model = choose_doc_type(RULES_CONTRACT, DocTypeGuess(DocType.SPEC, 0.95, "модель"))
    assert guess.doc_type is DocType.CONTRACT and not by_model


def test_model_types_document_without_signs() -> None:
    """Правила не нашли ничего — модель может только добавить документ под проверку."""
    guess, by_model = choose_doc_type(NO_SIGNS, DocTypeGuess(DocType.CONTRACT, 0.8, "модель"))
    assert guess.doc_type is DocType.CONTRACT and by_model


def test_agreement_raises_confidence() -> None:
    guess, by_model = choose_doc_type(RULES_CONTRACT, DocTypeGuess(DocType.CONTRACT, 0.8, "м"))
    assert guess.doc_type is DocType.CONTRACT and guess.confidence == 0.8 and by_model


# ═════════════════════════ шаг 1: закон закупки ═════════════════════════


def _law(value: str, conflict: str | None = None, *others: str) -> DocumentFacts:
    fact = FactValue(
        key="law",
        value=value,
        unit=None,
        raw=value,
        quote=f"Федеральным законом № {value}",
        chunk_id="c",
        document_id="d",
        doc_type=DocType.NOTICE,
        page_from=1,
        char_start=0,
        char_end=10,
        score=100.0,
        conflict={"value": conflict} if conflict else None,
        candidates=tuple({"value": o} for o in others),
    )
    return DocumentFacts("d", DocType.NOTICE, {"law": fact})


@pytest.mark.parametrize(
    ("documents", "expected"),
    [
        ([_law("44-ФЗ")], "44-ФЗ"),
        ([_law("44-ФЗ"), _law("44-ФЗ")], "44-ФЗ"),
        # модель прочитала «223-ФЗ» в оговорке, грамматика — «44-ФЗ»: закон не определён
        ([_law("223-ФЗ", "44-ФЗ")], None),
        # в тексте упомянуты оба закона
        ([_law("44-ФЗ", None, "223-ФЗ")], None),
        # документы называют разные законы
        ([_law("44-ФЗ"), _law("223-ФЗ")], None),
        ([], None),
    ],
)
def test_procurement_law_needs_agreement(
    documents: list[DocumentFacts], expected: str | None
) -> None:
    """Закон решает, какие правила проверять, — одного голоса мало; спорный закон = не определён,
    и применяются правила всех законов (а не ни одного)."""
    assert procurement_law(documents) == expected


# ═════════════════════════ шаг 2: структурная неизвестность ═════════════════════════


@pytest.mark.parametrize(
    ("text", "key", "raw"),
    [
        (
            "Расчёт с Поставщиком осуществляется по истечении 45 рабочих дней с даты приемки.",
            "payment_term_working_days",
            "45 рабочих дней",
        ),
        (
            "Денежные средства перечисляются Поставщику на 46-й рабочий день после приемки.",
            "payment_term_working_days",
            "46-й рабочий день",
        ),
        (
            "Срок оплаты второго этапа | два месяца с даты приемки",
            "payment_term_working_days",
            "два месяца",
        ),
        (
            "Документ о приемке подписывается не ранее чем через 25 рабочих дней с даты поставки.",
            "acceptance_term_working_days",
            "25 рабочих дней",
        ),
        (
            "Экспертиза второй партии проводится в течение сорока рабочих дней.",
            "acceptance_term_working_days",
            "сорока рабочих дней",
        ),
        (
            "Поставка осуществляется в течение 24 часов с момента заключения контракта.",
            "delivery_term_days",
            "24 часов",
        ),
        (
            "Обеспечение исполнения контракта устанавливается в размере половины цены контракта.",
            "contract_security_percent",
            "половины цены",
        ),
    ],
)
def test_unparsed_value_is_reported(text: str, key: str, raw: str) -> None:
    """Значение поля в незнакомой форме — не пропадает молча: «в порядке» по полю нельзя."""
    terms = unparsed_terms(key, [chunk("k1", "d", text)])
    assert [t.raw.lower() for t in terms] == [raw]


@pytest.mark.parametrize(
    "text",
    [
        "Поставщик за свой счет оплачивает доставку и осуществляет поставку в течение 15 рабочих "
        "дней.",
        "Поставщик осуществляет поставку товара по заявкам в течение 2 рабочих дней с момента "
        "получения заявки.",
        "Заказчик выплачивает аванс в течение 5 рабочих дней с даты заключения контракта.",
        "Счет выставляется в течение 3 рабочих дней с даты поставки.",
        "Документ о приемке подписывается в течение 5 рабочих дней. Оплата производится в течение "
        "7 рабочих дней с даты подписания документа о приемке.",
        "Гарантийный срок на Товар составляет 12 месяцев с даты подписания документа о приемке.",
        "Отказ возможен при однократной просрочке поставки на один день.",
        # год календарной даты — не срок «2026 лет» (syn-013, оценка 19.10.2026)
        "Обязательства по поставке Товара могут быть исполнены Поставщиком не позднее 15 "
        "октября 2026 года.",
        "Поставка Товара осуществляется до 31.12.2026 года.",
        "Поставка Товара осуществляется в 2026 году.",
        # цифры в скобках перед прописью (пакет ЕИС 0362100026126000023, 21.10.2026)
        "Заказчик осуществляет оплату не позднее (7) семи рабочих дней с даты подписания "
        "Заказчиком документа о приемке.",
        # сроки других действий (пакеты ЕИС, 21.10.2026): акт сверки, возврат обеспечения,
        # гарантийное обслуживание, уведомление о поставке
        "2.8. По окончании исполнения обязательств в течение 15 (пятнадцати) рабочих дней Стороны "
        "подписывают Акт сверки расчетов.",
        "Денежные средства, внесенные в качестве обеспечения исполнения Контракта, возвращаются "
        "Исполнителю в течение 15 (пятнадцати) дней с даты исполнения обязательств.",
        "Гарантийное обслуживание на поставленный Товар устанавливается сроком на 24 месяца с "
        "даты подписания документа о приемке.",
        "Поставщик не менее чем за 2 (два) рабочих дня до осуществления поставки Товара "
        "направляет в адрес Заказчика уведомление о времени и дате доставки Товара.",
    ],
)
def test_understood_values_are_not_unparsed(text: str) -> None:
    """Ложные тревоги, найденные замером (FP-05) и на синтетике: срок, понятый грамматикой и
    отнесённый к другому полю или действию, точка отсчёта «с даты поставки» и год календарной
    даты — не «неразобранные»."""
    chunks = [chunk("k1", "d", text)]
    keys = ("payment_term_working_days", "acceptance_term_working_days", "delivery_term_days")
    assert {k: [t.raw for t in unparsed_terms(k, chunks)] for k in keys} == {k: [] for k in keys}


# Настоящий пакет ЕИС 0338200008626000446 (масло, 21.10.2026): форму «в срок не более N рабочих
# дней, следующих за днем …» грамматика не знает, а модель прочитала её с проверенной цитатой
ACCEPTANCE_34 = (
    "3.4. Заказчик в срок не более 5 рабочих дней, следующих за днем поступления от Поставщика "
    "документов, на основании результатов экспертизы подписывает документ о приемке."
)


def _model_fact(key: str, value: float, source: SourceChunk, quote: str) -> DocumentFacts:
    start = source.text.index(quote)
    fact = FactValue(
        key,
        value,
        "working",
        None,
        quote,
        source.id,
        source.document_id,
        DocType.CONTRACT,
        1,
        source.char_start + start,
        source.char_start + start + len(quote),
        100.0,
    )
    return DocumentFacts(source.document_id, DocType.CONTRACT, {key: fact})


def _acc01(*chunks: SourceChunk, facts: DocumentFacts) -> RuleOutcome:
    ctx = CheckContext((view("d", DocType.CONTRACT, *chunks),), (facts,))
    return RuleEngine(EngineDeps(QuoteVerifier())).run([RULES["ACC-01"]], ctx)[0].outcome


def test_term_read_by_model_in_same_place_is_not_unreadable() -> None:
    """Срок в незнакомой грамматике форме, который модель прочитала в том же месте тем же числом:
    значение уже сравнивается с порогом. Факт до исправления: ACC-01 «срок не найден числом —
    проверьте вручную», хотя в карточке «5 раб. дн.» с цитатой этого пункта."""
    source = chunk("k34", "d", ACCEPTANCE_34)
    assert unparsed_terms("acceptance_term_working_days", [source])  # грамматика не взяла
    facts = _model_fact("acceptance_term_working_days", 5, source, "в срок не более 5 рабочих дней")
    assert _acc01(source, facts=facts) is RuleOutcome.OK


def test_term_of_other_field_is_not_unparsed_acceptance() -> None:
    """Пакет ЕИС 0107300002026000144: «Расчеты … в срок, не более 7 (семи) рабочих дней, с даты
    подписания Заказчиком документа о приемке» — срок оплаты (модель прочитала его здесь), а не
    неразобранный срок приёмки. Факт до исправления: ACC-01 «не найден числом» с цитатой оплаты."""
    payment = chunk(
        "k27",
        "d",
        "2.7. Расчеты между Заказчиком и Поставщиком производятся в срок, не более 7 (семи) "
        "рабочих дней, с даты подписания Заказчиком документа о приемке.",
    )
    acceptance = chunk(
        "k34", "d", "3.4. Приемка Товара осуществляется в течение 5 рабочих дней.", start=200
    )
    assert unparsed_terms("acceptance_term_working_days", [payment])  # тема «приемк» сработала
    read = extract_rule_facts("d", DocType.CONTRACT, [payment, acceptance])
    model = _model_fact("payment_term_working_days", 7, payment, "в срок, не более 7 (семи)")
    facts = replace(read, facts={**read.facts, **model.facts})
    assert _acc01(payment, acceptance, facts=facts) is RuleOutcome.OK


@pytest.mark.parametrize(
    ("text", "value", "words"),
    [
        ("Оплата производится не позднее (7) семи рабочих дней с даты приемки.", 7.0, None),
        # распознавание потеряло ноль: худшее значение — по прописи
        ("Оплата производится не позднее (3) тридцати рабочих дней с даты приемки.", 3.0, 30.0),
    ],
)
def test_digits_before_words_are_read(text: str, value: float, words: float | None) -> None:
    payment = extract_rule_facts("d", DocType.CONTRACT, [chunk("k1", "d", text)]).facts[
        "payment_term_working_days"
    ]
    assert payment.value == value
    assert [c["value"] for c in payment.candidates] == ([words] if words else [])


def test_article_part_is_not_a_term() -> None:
    """«пунктом 1 части 13 статьи 94» — не срок «1 час» (пакет ЕИС 0362100026126000023)."""
    text = (
        "Исполнитель размещает в единой информационной системе документ о приемке, который должен "
        "содержать информацию, предусмотренную пунктом 1 части 13 статьи 94 Закона."
    )
    assert unparsed_terms("acceptance_term_working_days", [chunk("k1", "d", text)]) == []


PAYMENT_7 = chunk("k24", "d", "2.4. Оплата производится в течение 7 рабочих дней с даты приемки.")


def _pay01(text: str) -> RuleOutcome:
    other = chunk("k27", "d", text, start=200)
    facts = extract_rule_facts("d", DocType.CONTRACT, [PAYMENT_7, other])
    ctx = CheckContext((view("d", DocType.CONTRACT, PAYMENT_7, other),), (facts,))
    return RuleEngine(EngineDeps(QuoteVerifier())).run([RULES["PAY-01"]], ctx)[0].outcome


@pytest.mark.parametrize(
    "text",
    [
        # пакет ЕИС 0362100026126000023, п. 2.2.7: срок уведомления о смене расчётного счёта
        "2.2.7. При изменении расчетного счета Исполнитель обязан уведомить Заказчика "
        "в течение 3 (трех) рабочих дней.",
        "Документ о приемке и счет на оплату не позднее одного часа направляются Заказчику.",
    ],
)
def test_unparsed_term_that_cannot_be_a_risk_does_not_block(text: str) -> None:
    """Неразобранный срок, который ни при каком толковании (рабочие или календарные дни, часы)
    не превышает порог, не меняет вывод: «в порядке» по прочитанному сроку оплаты допустимо.
    Факт до исправления: PAY-01 «срок оплаты не найден числом» при прочитанных 7 днях."""
    assert unparsed_terms("payment_term_working_days", [chunk("k", "d", text)])
    assert _pay01(text) is RuleOutcome.OK


@pytest.mark.parametrize(
    "text",
    [
        "2.3. Расчёт за второй этап осуществляется по истечении 45 рабочих дней с даты приемки.",
        # «15 дней» без вида: как рабочие — 15 > 7, значит, риск возможен
        "2.5. Расчет за второй этап производится по истечении 15 (пятнадцати) дней с даты приемки.",
    ],
)
def test_unparsed_term_that_may_be_a_risk_still_blocks(text: str) -> None:
    """Контроль безопасности зелёного: срок, который мог бы дать риск, по-прежнему «не найден
    числом — проверьте вручную»."""
    assert _pay01(text) is RuleOutcome.ABSENT


def test_decimal_point_does_not_split_sentence() -> None:
    """«частями 7, 7.1 и 7.2 статьи 96 … возвращается в течение 15 дней» (пакет ЕИС
    0362100026126000023): точка в номере части резала предложение, и возврат обеспечения
    читался как неразобранный срок оплаты. Факт до исправления: PAY-01 «проверьте вручную»."""
    text = (
        "Обеспечение исполнения Контракта с учетом уменьшения его размера в соответствии с "
        "частями 7, 7.1 и 7.2 статьи 96 Закона возвращается Исполнителю в течение 15 "
        "(пятнадцати) дней с даты исполнения обязательств (после подписания документа о приемке "
        "за последний расчетный период)."
    )
    assert unparsed_terms("payment_term_working_days", [chunk("k", "d", text)]) == []
    assert _pay01(text) is RuleOutcome.OK


@pytest.mark.parametrize(
    ("key", "text"),
    [
        # срок поставки, отсчитанный от уведомления, — срок поставки
        (
            "delivery_term_days",
            "Отгрузка Товара производится по истечении 45 дней с момента уведомления Заказчика.",
        ),
        # оплата с упоминанием обеспечения — по-прежнему тема оплаты
        (
            "payment_term_working_days",
            "Оплата и возврат обеспечения исполнения Контракта производятся по истечении 45 "
            "рабочих дней.",
        ),
        # приёмка рядом с гарантией, но гарантийного срока в месяцах нет
        (
            "acceptance_term_working_days",
            "Приемка с проверкой гарантийных документов проводится по истечении 45 рабочих дней.",
        ),
    ],
)
def test_other_subject_exclusions_keep_real_terms(key: str, text: str) -> None:
    """Контроль безопасности зелёного: исключения «чужих» сроков узкие — настоящий срок поля в
    незнакомой форме по-прежнему не теряется."""
    assert unparsed_terms(key, [chunk("k1", "d", text)])


SERVICES = (
    "ФГБУН «Институт», именуемое в дальнейшем «Заказчик», и ООО «Чистота», именуемое в "
    "дальнейшем «Исполнитель», заключили настоящий Контракт. 1.1. Исполнитель обязуется оказать "
    "услуги по уборке помещений. Срок оказания услуг: с 01 января по 31 декабря 2027 года."
)
WORKS = (
    "Префектура, именуемая в дальнейшем «Заказчик», и «Подрядчик» заключили настоящий Контракт. "
    "1.1. Подрядчик выполняет текущий ремонт помещений. Окончание работ: 01.12.2026."
)
GOODS = (
    "ГКУ «Заказчик» и ООО «Ромашка», именуемое в дальнейшем «Поставщик», заключили настоящий "
    "Контракт. 1.1. Поставщик обязуется поставить бумагу офисную."
)


def _term01(text: str) -> tuple[RuleOutcome, dict[str, Any] | None]:
    source = chunk("k1", "d", text)
    facts = extract_rule_facts("d", DocType.CONTRACT, [source])
    ctx = CheckContext((view("d", DocType.CONTRACT, source),), (facts,))
    result = RuleEngine(EngineDeps(QuoteVerifier())).run([RULES["TERM-01"]], ctx)[0]
    return result.outcome, result.details


@pytest.mark.parametrize("text", [SERVICES, WORKS])
def test_no_delivery_term_in_services_or_works_is_not_a_finding(text: str) -> None:
    """Пакеты ЕИС 0362100026126000023 (уборка) и 0373200032226000791 (ремонт): поставки товара
    нет, сторона — «Исполнитель» или «Подрядчик». Факт до исправления: TERM-01 «срок поставки
    не найден числом — проверьте вручную» (ложное замечание). Ожидается: «не проверено» по
    существу (не сбой — зелёный не блокируется)."""
    outcome, details = _term01(text)
    assert outcome is RuleOutcome.NOT_CHECKED and not is_failure(details)


@pytest.mark.parametrize(
    "text",
    [
        GOODS,
        # поставка с услугами: «Поставщик» есть — срок поставки обязателен
        GOODS.replace("бумагу офисную.", "оборудование и оказать услуги «Исполнитель» монтажа."),
    ],
)
def test_no_delivery_term_in_goods_contract_is_still_a_finding(text: str) -> None:
    """Контроль: в контракте поставки срок поставки — существенное условие, «не найден» —
    по-прежнему замечание «проверьте вручную»."""
    assert _term01(text)[0] is RuleOutcome.ABSENT


def test_term_read_elsewhere_or_with_other_number_stays_unreadable() -> None:
    """Контроль безопасности зелёного: модель прочитала срок в другом пункте или другим числом —
    неразобранное место остаётся «не найдено числом», «в порядке» по полю нельзя."""
    other_text = "3.5. Приемка осуществляется в течение 5 рабочих дней."
    other_place = chunk("k35", "d", other_text, start=len(ACCEPTANCE_34))
    source = chunk("k34", "d", ACCEPTANCE_34.replace("5 рабочих", "45 рабочих"))
    elsewhere = _model_fact("acceptance_term_working_days", 5, other_place, "5 рабочих дней")
    assert _acc01(source, other_place, facts=elsewhere) is RuleOutcome.ABSENT
    misread = _model_fact("acceptance_term_working_days", 5, source, "в срок не более 45 рабочих")
    assert _acc01(source, facts=misread) is RuleOutcome.ABSENT


def test_mandatory_liability_topics_are_data() -> None:
    """Обязательные темы — данные набора (критерий «новое правило без изменения кода»): штрафы и
    пеня поставщика (ч. 7, 8 ст. 34) — «не найдено» даёт замечание; односторонний отказ (ч. 14
    ст. 34 — «может быть включено») — нет."""
    rules = {r.id: r for r in RULES.values() if isinstance(r, LlmCheckRule)}
    assert rules["LIA-01"].absent_behavior is AbsentBehavior.REPORT
    assert rules["LIA-03"].absent_behavior is AbsentBehavior.REPORT
    assert rules["TRM-01"].absent_behavior is AbsentBehavior.IGNORE
    assert rules["TRM-02"].absent_behavior is AbsentBehavior.IGNORE


# все исходы, которые бывают у правила, — с признаками в details, как их пишет движок
_OUTCOMES: list[tuple[RuleOutcome, dict[str, Any] | None, Severity | None]] = [
    (RuleOutcome.VIOLATION, None, Severity.RED),
    (RuleOutcome.VIOLATION, None, Severity.YELLOW),
    (RuleOutcome.ABSENT, None, Severity.YELLOW),
    (RuleOutcome.OK, {"basis": "evidence"}, None),
    (RuleOutcome.OK, {"basis": "absence"}, None),
    (RuleOutcome.OK, {"basis": "unconfirmed"}, None),
    (RuleOutcome.NOT_CHECKED, {"error": "unavailable"}, None),
    (RuleOutcome.NOT_CHECKED, {"error": "unverified_ok"}, None),
    (RuleOutcome.NOT_CHECKED, {"blocked": "unreadable"}, None),
    (RuleOutcome.NOT_CHECKED, {"blocked": "partial_search"}, None),
    (RuleOutcome.NOT_CHECKED, {"blocked": "red_flag"}, None),
    (RuleOutcome.NOT_CHECKED, {"missing_documents": ["contract"]}, None),
    (RuleOutcome.NOT_CHECKED, None, None),  # по существу: другой закон, значение не указано
]


def test_unknown_never_becomes_green() -> None:
    """Свойство: правило «критично» с неизвестным исходом, замечание или неполный текст — зелёного
    нет ни при одном сочетании. Перебор: 3 правила (два «критично», одно «внимание») × 13 исходов
    × полнота текста — 4394 сочетания."""
    red = {"R1", "R2"}
    green_seen = 0
    for combo in itertools.product(_OUTCOMES, repeat=3):
        results = list(zip(("R1", "R2", "Y1"), combo, strict=True))
        severities = [sev for _, (_, _, sev) in results if sev is not None]
        assessed = sum(counts_as_assessed(outcome, details) for _, (outcome, details, _) in results)
        gaps = critical_unchecked(((c, o, d) for c, (o, d, _) in results), red)
        for incomplete in (0, 1):
            light = package_verdict(
                [light_from_severities(severities)], [], assessed, 3, len(gaps), incomplete
            )
            if light is not Light.GREEN:
                continue
            green_seen += 1
            assert not severities and not incomplete, combo
            for code, (outcome, details, _) in results:
                if code in red:
                    assert not (outcome is RuleOutcome.NOT_CHECKED and is_failure(details)), combo
    assert green_seen > 0  # зелёный достижим — свойство не выполнено «вхолостую»
