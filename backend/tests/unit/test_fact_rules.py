"""Детерминированный канал карточки: формулировки из живых контрактов, ловушки, сверка каналов."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from svetofor.db.enums import DocType
from svetofor.eval.synthetic import generate
from svetofor.pipeline.chunking import make_chunks
from svetofor.pipeline.doctype import detect_doc_type
from svetofor.pipeline.extract import extract_document
from svetofor.pipeline.structure import build_structure
from svetofor.rules.fact_rules import extract_rule_facts, merge_channels, same_value
from svetofor.rules.facts import DocumentFacts, SourceChunk, merge_card
from svetofor.rules.facts_catalog import FACTS, FactKind
from svetofor.rules.loader import load_ruleset_file


def facts_of(text: str, doc_type: DocType = DocType.CONTRACT) -> dict[str, Any]:
    chunk = SourceChunk("c1", "d1", "Контракт", 1, 1, text, 1000)
    return extract_rule_facts("d1", doc_type, [chunk]).facts


@pytest.mark.parametrize(
    ("text", "key", "value", "unit"),
    [
        (
            "Оплата производится в течение 7 (семи) рабочих дней с даты подписания "
            "документа о приёмке.",
            "payment_term_working_days",
            7,
            "working",
        ),
        (
            "Оплата поставленного товара осуществляется не позднее 10 рабочих дней "
            "с даты подписания Заказчиком документа о приемке.",
            "payment_term_working_days",
            10,
            "working",
        ),
        (
            "Оплата осуществляется в срок не более 30 календарных дней с даты поставки.",
            "payment_term_working_days",
            30,
            "calendar",
        ),
        (
            "Оплата по Контракту производится в течение 15 дней со дня подписания акта.",
            "payment_term_working_days",
            15,
            "working",
        ),
        (
            "Приёмка товара осуществляется в течение 5 (пяти) рабочих дней с даты поставки.",
            "acceptance_term_working_days",
            5,
            "working",
        ),
        (
            "Документ о приемке подписывается Заказчиком не позднее 20 рабочих дней.",
            "acceptance_term_working_days",
            20,
            "working",
        ),
        (
            "Срок поставки: в течение 30 (тридцати) календарных дней с даты заключения контракта.",
            "delivery_term_days",
            30,
            "calendar",
        ),
        (
            "Поставка Товара осуществляется в течение 10 рабочих дней с даты заключения Контракта.",
            "delivery_term_days",
            10,
            "working",
        ),
        ("Авансирование не предусмотрено.", "advance_percent", 0, None),
        ("Аванс не выплачивается.", "advance_percent", 0, None),
        (
            "Заказчик выплачивает аванс в размере 30 (тридцати) процентов цены Контракта.",
            "advance_percent",
            30,
            None,
        ),
        (
            "Размер обеспечения исполнения Контракта составляет 5 процентов цены Контракта.",
            "contract_security_percent",
            5,
            None,
        ),
        (
            "Обеспечение исполнения контракта устанавливается в размере 10% от цены контракта.",
            "contract_security_percent",
            10,
            None,
        ),
        (
            "Гарантийный срок на Товар составляет не менее 12 (двенадцати) месяцев.",
            "warranty_months",
            12,
            None,
        ),
        (
            "Гарантийный срок — 2 (два) года с даты подписания документа о приёмке.",
            "warranty_months",
            24,
            None,
        ),
        (
            "Поставщик обязуется поставить товар в количестве 500 (пятьсот) пачек.",
            "quantity_total",
            500,
            "пачек",
        ),
    ],
)
def test_contract_phrasings(text: str, key: str, value: float, unit: str | None) -> None:
    found = facts_of(text)
    assert key in found, text
    assert found[key].value == value and found[key].unit == unit
    assert found[key].quote.strip() == text  # цитата — предложение целиком, точно из документа
    assert found[key].source == "rules" and found[key].score == 100.0


@pytest.mark.parametrize(
    ("text", "key", "value"),
    [
        ("Начальная (максимальная) цена контракта: 1 250 000,00 рублей.", "nmck_rub", 1250000),
        ("Номер извещения: № 0373100012326000123.", "procurement_number", "0373100012326000123"),
        (
            "Закупка проводится в соответствии с Федеральным законом от 05.04.2013 № 44-ФЗ.",
            "law",
            "44-ФЗ",
        ),
        (
            "Установлен запрет допуска товаров, происходящих из иностранных государств.",
            "national_regime",
            "ban",
        ),
        (
            "Ограничения допуска товаров, происходящих из иностранных государств, не установлены.",
            "national_regime",
            "none",
        ),
        (
            "Участниками закупки могут быть только субъекты малого предпринимательства.",
            "smp_only",
            True,
        ),
        (
            "Размер обеспечения заявки составляет 1 процент начальной (максимальной) "
            "цены контракта.",
            "bid_security_percent",
            1,
        ),
        ("Обеспечение гарантийных обязательств не требуется.", "warranty_security_percent", 0),
    ],
)
def test_notice_phrasings(text: str, key: str, value: object) -> None:
    found = facts_of(text, DocType.NOTICE)
    assert key in found and found[key].value == value, text


def test_security_not_required_only_for_some_participants_is_not_zero() -> None:
    """Оговорка ч. 8 ст. 96 44-ФЗ («если победитель — казённое учреждение, обеспечение не
    требуется») стоит почти в каждом извещении. Это не «обеспечение 0 %»: иначе ложное
    противоречие CON-04 с 10 % в контракте (закупка ЕИС 0120100008026000013, 30.10.2026)."""
    for text in (
        "В случае, если участником аукциона, с которым заключается контракт, является казенное "
        "учреждение, обеспечение исполнения контракта не требуется.",
        "Если победитель — казенное учреждение, обеспечение исполнения контракта не требуется.",
        "При условии, что участник — казенное учреждение, обеспечение исполнения контракта "
        "не требуется.",
    ):
        found = facts_of(text, DocType.NOTICE)
        assert "contract_security_percent" not in found, text
    plain = facts_of("Обеспечение исполнения контракта не требуется.", DocType.NOTICE)
    assert plain["contract_security_percent"].value == 0
    # условие в предыдущем предложении не мешает
    after = facts_of(
        "Если участник — казенное учреждение, обеспечение не требуется. "
        "Обеспечение исполнения контракта не установлено.",
        DocType.NOTICE,
    )
    assert after["contract_security_percent"].value == 0


@pytest.mark.parametrize(
    ("text", "absent"),
    [
        # оплата аванса — не срок оплаты товара
        (
            "Оплата аванса производится в течение 10 рабочих дней с даты заключения Контракта.",
            "payment_term_working_days",
        ),
        # срок исполнения заявки — не срок поставки
        (
            "Поставка осуществляется по заявкам. Срок исполнения заявки — 2 рабочих дня.",
            "delivery_term_days",
        ),
        # срок в часах — не дни
        ("Срок поставки — в течение 24 часов с момента заявки.", "delivery_term_days"),
        # обеспечение гарантийных обязательств — не обеспечение исполнения контракта
        (
            "Обеспечение гарантийных обязательств составляет 10 процентов цены.",
            "contract_security_percent",
        ),
        # даты — не сроки
        ("Контракт действует до 31.12.2026.", "delivery_term_days"),
    ],
)
def test_traps(text: str, absent: str) -> None:
    assert absent not in facts_of(text, DocType.CONTRACT) | facts_of(text, DocType.NOTICE)


def test_only_facts_of_document_type() -> None:
    text = (
        "Размер обеспечения заявки составляет 1 процент. "
        "Оплата производится в течение 7 рабочих дней."
    )
    assert "bid_security_percent" not in facts_of(text, DocType.CONTRACT)  # заявка — из извещения
    assert "payment_term_working_days" not in facts_of(text, DocType.NOTICE)


def test_first_occurrence_wins_and_offsets_exact() -> None:
    text = (
        "2.1. Оплата производится в течение 7 рабочих дней с даты приёмки. "
        "2.2. Оплата неустойки производится в течение 30 рабочих дней."
    )
    chunk = SourceChunk("c1", "d1", "Контракт", 2, 2, text, 500)
    fact = extract_rule_facts("d1", DocType.CONTRACT, [chunk]).facts["payment_term_working_days"]
    assert fact.value == 7 and text[fact.char_start - 500 : fact.char_end - 500] == fact.quote


def test_quote_does_not_take_section_heading() -> None:
    """Совпадение начинается сразу за номером пункта — заголовок раздела в цитату не попадает."""
    text = (
        "2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ\n"
        "2.1. Оплата производится в течение 15 рабочих дней с даты подписания\n"
        "документа о приёмке. Аванс не предусмотрен."
    )
    found = facts_of(text)
    quote = found["payment_term_working_days"].quote
    assert quote == (
        "2.1. Оплата производится в течение 15 рабочих дней с даты подписания\nдокумента о приёмке."
    )
    assert found["advance_percent"].quote == "Аванс не предусмотрен."


def test_quote_stops_before_next_clause_without_period() -> None:
    text = (
        "3.1. Оплата производится в течение 7 рабочих дней с даты приёмки\n"
        "3.2. Приёмка проводится в течение 5 рабочих дней."
    )
    quote = facts_of(text)["payment_term_working_days"].quote
    assert quote == "3.1. Оплата производится в течение 7 рабочих дней с даты приёмки"


# ─── Сверка каналов ───


def _doc(facts: dict[str, Any], error: str | None = None) -> DocumentFacts:
    return DocumentFacts("d1", DocType.CONTRACT, facts, error=error)


def test_channels_agree() -> None:
    rules = facts_of("Оплата производится в течение 15 рабочих дней с даты приёмки.")
    model = {
        "payment_term_working_days": replace(
            rules["payment_term_working_days"],
            source="llm",
            quote="в течение 15 рабочих дней",
            score=97.0,
        )
    }
    merged = merge_channels(_doc(rules), _doc(model))
    fact = merged.facts["payment_term_working_days"]
    assert fact.source == "both" and fact.conflict is None
    assert fact.quote.startswith("Оплата производится")  # цитата — предложение целиком


def test_channels_disagree_conflict_visible() -> None:
    rules = facts_of("Оплата производится в течение 15 рабочих дней с даты приёмки.")
    model_fact = replace(
        rules["payment_term_working_days"], value=30.0, source="llm", quote="другая цитата"
    )
    merged = merge_channels(_doc(rules), _doc({"payment_term_working_days": model_fact}))
    fact = merged.facts["payment_term_working_days"]
    assert fact.value == 30.0 and fact.source == "llm"
    assert fact.conflict is not None and fact.conflict["value"] == 15.0
    assert fact.conflict["quote"].startswith("Оплата производится")


def test_rules_work_without_model() -> None:
    rules = facts_of("Оплата производится в течение 15 рабочих дней с даты приёмки.")
    merged = merge_channels(_doc(rules), _doc({}, error="нет доступа к модели"))
    assert merged.facts["payment_term_working_days"].source == "rules"
    assert merged.error == "нет доступа к модели"  # честно: модель не участвовала


def test_units_matter_for_days() -> None:
    working = facts_of("Срок поставки — в течение 10 рабочих дней.")["delivery_term_days"]
    calendar = replace(working, unit="calendar")
    assert not same_value(working, calendar) and same_value(working, replace(working, unit=None))


def test_synthetic_set_rules_channel_accuracy(tmp_path: Path) -> None:
    """Канал грамматик на всём синтетическом наборе через настоящий конвейер (PDF → фрагменты)."""
    rules = load_ruleset_file(
        Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
    ).rules
    total = correct = 0
    misses: list[str] = []
    for folder in generate(tmp_path, rules, "pdf"):
        expected = yaml.safe_load((folder / "expected.yaml").read_text(encoding="utf-8"))["facts"]
        documents = []
        for pdf in sorted(folder.glob("*.pdf")):
            doc = extract_document(pdf)
            structure = build_structure(doc, pdf.stem)
            doc_type = detect_doc_type(pdf.name, structure.title, doc.pages[0].text).doc_type
            chunks = [
                SourceChunk(
                    f"{pdf.stem}-{c.order}",
                    pdf.stem,
                    c.section_path,
                    c.page_from,
                    c.page_to,
                    c.text,
                    c.char_start,
                    c.token_count,
                )
                for c in make_chunks(doc, structure)
            ]
            documents.append(extract_rule_facts(pdf.stem, doc_type, chunks))
        card = merge_card(documents)
        for key, value in expected.items():
            if FACTS[key].kind is FactKind.TEXT:
                continue
            total += 1
            actual = card.get(key)
            if actual is not None and actual.value == value:
                correct += 1
            else:
                misses.append(f"{folder.name}:{key}={value}≠{actual.value if actual else None}")
    assert total > 100
    assert correct == total, misses


def test_grammar_reads_text_with_lookalike_letters() -> None:
    """«Оплaта» с латинской «a» — грамматика всё равно находит срок, цитата — как в документе."""
    found = facts_of("Оплaта производится в течение 7 рабочих дней с даты приёмки.")
    fact = found["payment_term_working_days"]
    assert fact.value == 7 and fact.quote.startswith("Оплaта")


@pytest.mark.parametrize(
    ("words", "value"),
    [
        ("десяти", 10),
        ("двадцати пяти", 25),
        ("сто одиннадцать", 111),
        ("сто сорока трех", 143),
        ("пять двадцать", None),  # порядок разрядов нарушен
        ("двадцать одиннадцать", None),
    ],
)
def test_parse_numeral(words: str, value: int | None) -> None:
    from svetofor.rules.fact_rules import parse_numeral

    assert parse_numeral(words) == value


@pytest.mark.parametrize(
    ("text", "key", "value", "unit"),
    [
        (
            "Оплата производится в течение десяти рабочих дней.",
            "payment_term_working_days",
            10,
            "working",
        ),
        (
            "Оплата производится в течение пятнадцати (15) рабочих дней.",
            "payment_term_working_days",
            15,
            "working",
        ),
        (
            "Срок поставки: в течение двадцати пяти календарных дней с даты заключения контракта.",
            "delivery_term_days",
            25,
            "calendar",
        ),
        (
            "Оплата производится в течение одного рабочего дня.",
            "payment_term_working_days",
            1,
            "working",
        ),
    ],
)
def test_spelled_out_terms(text: str, key: str, value: float, unit: str) -> None:
    fact = facts_of(text)[key]
    assert fact.value == value and fact.unit == unit and fact.quote == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Дата и время окончания срока подачи заявок: 15.10.2026 10:00 (МСК)",
            "15.10.2026 10:00",
        ),
        (
            "Окончание срока подачи заявок — 03.11.2026 г. в 09.00 по местному времени.",
            "03.11.2026 09:00",
        ),
        ("Дата окончания срока подачи заявок 20.10.2026", "20.10.2026"),
    ],
)
def test_application_deadline_from_notice(text: str, expected: str) -> None:
    fact = facts_of(text, DocType.NOTICE)["application_deadline"]
    assert fact.value == expected


def test_application_deadline_only_from_notice() -> None:
    text = "Дата и время окончания срока подачи заявок: 15.10.2026 10:00"
    assert "application_deadline" not in facts_of(text, DocType.CONTRACT)
