"""Определение типа документа: имя файла, название, первая страница, спорные случаи."""

from __future__ import annotations

import pytest

from svetofor.db.enums import DOC_TYPE_TITLES, DocType
from svetofor.pipeline.doctype import detect_doc_type

CONTRACT_PAGE = (
    "ГКУ «Заказчик», именуемое в дальнейшем «Заказчик», и участник закупки, именуемый в "
    "дальнейшем «Поставщик», заключили настоящий контракт. 1. Предмет контракта"
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Проект контракта.docx", DocType.CONTRACT),
        ("ПРОЕКТ_ГОСУДАРСТВЕННОГО_КОНТРАКТА.pdf", DocType.CONTRACT),
        ("proekt_kontrakta.docx", DocType.CONTRACT),
        ("Описание объекта закупки.docx", DocType.SPEC),
        ("ТЗ на поставку бумаги.doc", DocType.SPEC),
        ("Техническое задание.pdf", DocType.SPEC),
        ("Приложение 1 к контракту - Спецификация.docx", DocType.SPEC),
        ("Обоснование НМЦК.xlsx", DocType.NMCK),
        ("Обоснование начальной (максимальной) цены контракта.docx", DocType.NMCK),
        ("Расчёт цены контракта.xlsx", DocType.NMCK),
        ("Требования к содержанию, составу заявки.docx", DocType.BID_REQUIREMENTS),
        ("Инструкция по заполнению заявки.rtf", DocType.BID_REQUIREMENTS),
        ("Извещение о проведении электронного аукциона.pdf", DocType.NOTICE),
        ("Извещение 0373100012326000123.pdf", DocType.NOTICE),
        ("Документация о закупке.docx", DocType.NOTICE),
        ("архив/папка/Проект контракта.docx", DocType.CONTRACT),
    ],
)
def test_by_filename(filename: str, expected: DocType) -> None:
    guess = detect_doc_type(filename, None, "")
    assert guess.doc_type == expected
    assert guess.certain


def test_title_and_body_when_filename_says_nothing() -> None:
    guess = detect_doc_type("Приложение 2.docx", "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ", "Техническое задание")
    assert guess.doc_type == DocType.SPEC
    assert guess.confidence >= 0.9
    assert "название документа" in guess.reason


def test_attribution_to_notice_is_not_a_notice() -> None:
    page = "Приложение № 3 к извещению об осуществлении закупки. " + CONTRACT_PAGE
    guess = detect_doc_type("Приложение 3.docx", "ПРОЕКТ КОНТРАКТА", page)
    assert guess.doc_type == DocType.CONTRACT and guess.certain


def test_state_contract_abbreviation_and_body() -> None:
    """Пакет ЕИС 0173100012626000097 (ФССП, 21.10.2026): «Приложение № 4 Проект ГК (1).doc» с
    терминами «Техническое задание … требования к качеству» на первой странице. Факт до
    исправления: тип «ТЗ» — контракт выпал из правил контракта, LIA-02 «нет ответственности
    заказчика» стало красным по чужому документу."""
    page = (
        "Приложение № 4 к Извещению об осуществлении закупки ПРОЕКТ Государственный контракт "
        "№ ____ на поставку защищенных абонентских пунктов. ФССП России, именуемая в дальнейшем "
        "«Заказчик», заключили настоящий Государственный контракт. ОСНОВНЫЕ ТЕРМИНЫ "
        "Техническое задание – документ, содержащий требования к качеству, техническим и "
        "функциональным характеристикам товара."
    )
    guess = detect_doc_type("Приложение № 4 Проект ГК  (1).doc", None, page)
    assert guess.doc_type == DocType.CONTRACT and guess.certain


def test_body_only_is_uncertain() -> None:
    guess = detect_doc_type("документ.pdf", None, "Стороны, именуемые в дальнейшем…")
    assert guess.doc_type == DocType.CONTRACT
    assert not guess.certain  # один слабый признак — нужно подтверждение


def test_body_strong_enough() -> None:
    guess = detect_doc_type("документ.pdf", None, CONTRACT_PAGE)
    assert guess.doc_type == DocType.CONTRACT
    assert 0.4 < guess.confidence < 0.9


def test_conflict_prefers_title_but_uncertain() -> None:
    guess = detect_doc_type("Проект контракта.docx", "ТЕХНИЧЕСКОЕ ЗАДАНИЕ", "")
    assert guess.doc_type == DocType.SPEC
    assert not guess.certain
    assert "признаки другого типа" in guess.reason


def test_nothing_found() -> None:
    guess = detect_doc_type("scan_001.pdf", None, "Просто текст без признаков.")
    assert guess.doc_type == DocType.OTHER and guess.confidence == 0.0


def test_every_type_has_russian_title() -> None:
    assert set(DOC_TYPE_TITLES) == set(DocType)
