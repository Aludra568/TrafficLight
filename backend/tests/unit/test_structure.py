"""Разбор структуры: разделы, пункты, приложения и защита от ложных заголовков."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from svetofor.pipeline.extract import ExtractedDocument, extract_document
from svetofor.pipeline.structure import DocumentStructure, SectionKind, build_structure
from tests.support.pdfgen import PdfLine, PdfSpec, build_pdf, pages_from_markup

CONTRACT = """
!! ПРОЕКТ КОНТРАКТА
№ ____ на поставку бумаги для офисной техники
г. Москва «___» ________ 2026 г.
ГКУ «Заказчик», именуемое в дальнейшем «Заказчик», с одной стороны,
и участник закупки, именуемый в дальнейшем «Поставщик», заключили Контракт:
**1. ПРЕДМЕТ КОНТРАКТА**
1.1. Поставщик обязуется поставить бумагу для офисной техники (далее — Товар),
а Заказчик обязуется принять и оплатить Товар на условиях Контракта.
1.2. Количество Товара определяется Спецификацией (Приложение № 1).
**2. ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ**
2.1. Цена Контракта составляет 1 200 000,00 рублей и включает расходы
Поставщика на доставку Товара, которая осуществляется в течение
10 (десяти) рабочих дней с даты заключения Контракта.
2.2. Оплата производится в течение 7 рабочих дней с даты подписания
документа о приёмке, предусмотренного пунктом
3.1 Контракта.
2.3. Авансирование не предусмотрено.
===
**3. ПОРЯДОК ПРИЁМКИ**
3.1. Приёмка осуществляется в срок не более 5 рабочих дней. Акт подписывается
15.01.2026 года или в иной согласованный Сторонами срок.
3.2. Документы, необходимые для приёмки:
1. Счёт-фактура;
2. Товарная накладная.
**4. ОТВЕТСТВЕННОСТЬ СТОРОН**
4.1. Пеня начисляется за каждый день просрочки исполнения обязательства.
4.2. Порядок начисления неустойки установлен
Статья 34 Федерального закона № 44-ФЗ применяется в части, не урегулированной,
настоящим Контрактом.
===
Приложение № 1
к Контракту № ____
!! СПЕЦИФИКАЦИЯ
**Наименование** | **Ед. изм.** | **Количество**
1 | Бумага А4 | 100
**Итого:**
"""


def structure_of(tmp_path: Path, markup: str, root: str = "Проект контракта", **kw: object):
    pdf = build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / "doc.pdf")
    doc = extract_document(pdf)
    return doc, build_structure(doc, root, **kw)  # type: ignore[arg-type]


def labels(structure: DocumentStructure) -> list[tuple[int, str]]:
    return [(n.level, n.label) for n in structure.nodes]


def assert_partition(doc: ExtractedDocument, structure: DocumentStructure) -> None:
    """Собственные отрезки узлов без пропусков и наложений покрывают весь текст."""
    nodes = structure.nodes
    assert nodes[0].char_start == 0
    for a, b in itertools.pairwise(nodes):
        assert a.own_end == b.char_start
    assert nodes[-1].own_end == len(doc.text)
    for node in nodes:
        assert node.char_start <= node.own_end <= node.char_end
        if node.parent is not None:
            parent = nodes[node.parent]
            assert parent.char_start < node.char_start and node.char_end <= parent.char_end
            assert node.level == parent.level + 1


def test_contract_tree(tmp_path: Path) -> None:
    doc, structure = structure_of(tmp_path, CONTRACT)
    assert structure.title == "ПРОЕКТ КОНТРАКТА"
    assert labels(structure) == [
        (1, "Вводная часть"),
        (1, "1. ПРЕДМЕТ КОНТРАКТА"),
        (2, "1.1"),
        (2, "1.2"),
        (1, "2. ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ"),
        (2, "2.1"),
        (2, "2.2"),
        (2, "2.3"),
        (1, "3. ПОРЯДОК ПРИЁМКИ"),
        (2, "3.1"),
        (2, "3.2"),
        (1, "4. ОТВЕТСТВЕННОСТЬ СТОРОН"),
        (2, "4.1"),
        (2, "4.2"),
        (1, "Приложение № 1"),
        (2, "СПЕЦИФИКАЦИЯ"),
    ]
    assert_partition(doc, structure)


def test_false_headings_stay_inside_clauses(tmp_path: Path) -> None:
    doc, structure = structure_of(tmp_path, CONTRACT)
    by_label = {n.label: n for n in structure.nodes}
    own = {label: doc.text[n.char_start : n.own_end] for label, n in by_label.items()}
    assert "10 (десяти) рабочих дней" in own["2.1"]
    assert "3.1 Контракта." in own["2.2"]
    assert "15.01.2026 года" in own["3.1"]
    assert "1. Счёт-фактура;" in own["3.2"] and "2. Товарная накладная." in own["3.2"]
    assert "Статья 34 Федерального закона" in own["4.2"]
    assert "Наименование" in own["СПЕЦИФИКАЦИЯ"] and "Итого:" in own["СПЕЦИФИКАЦИЯ"]


def test_kinds_numbers_paths_pages(tmp_path: Path) -> None:
    _, structure = structure_of(tmp_path, CONTRACT)
    by_label = {n.label: n for n in structure.nodes}
    clause = by_label["4.1"]
    assert clause.kind == SectionKind.CLAUSE and clause.number == "4.1" and clause.title == ""
    assert clause.path == "Проект контракта › 4. ОТВЕТСТВЕННОСТЬ СТОРОН › 4.1"
    heading = by_label["3. ПОРЯДОК ПРИЁМКИ"]
    assert heading.kind == SectionKind.HEADING and heading.number == "3"
    assert heading.title == "ПОРЯДОК ПРИЁМКИ"
    assert (heading.page_from, heading.page_to) == (2, 2)
    assert by_label["Приложение № 1"].kind == SectionKind.APPENDIX
    assert by_label["СПЕЦИФИКАЦИЯ"].path == "Проект контракта › Приложение № 1 › СПЕЦИФИКАЦИЯ"
    assert by_label["Вводная часть"].kind == SectionKind.PREAMBLE
    assert by_label["2. ЦЕНА КОНТРАКТА И ПОРЯДОК РАСЧЁТОВ"].page_to == 1


def test_node_at_offset(tmp_path: Path) -> None:
    doc, structure = structure_of(tmp_path, CONTRACT)
    node = structure.node_at(doc.text.index("Пеня начисляется"))
    assert node is not None and node.label == "4.1"
    first = structure.node_at(0)
    assert first is not None and first.kind == SectionKind.PREAMBLE


def test_multiline_bold_heading_merged(tmp_path: Path) -> None:
    markup = """
**1. ОБЩИЕ ПОЛОЖЕНИЯ**
1.1. Настоящий Контракт заключён по итогам электронного аукциона.
**2. ПОРЯДОК РАЗРЕШЕНИЯ СПОРОВ И ИНЫЕ**
**УСЛОВИЯ КОНТРАКТА**
2.1. Споры разрешаются в Арбитражном суде.
"""
    _, structure = structure_of(tmp_path, markup)
    assert labels(structure) == [
        (1, "1. ОБЩИЕ ПОЛОЖЕНИЯ"),
        (2, "1.1"),
        (1, "2. ПОРЯДОК РАЗРЕШЕНИЯ СПОРОВ И ИНЫЕ УСЛОВИЯ КОНТРАКТА"),
        (2, "2.1"),
    ]


def test_articles_and_plain_numbered_headings(tmp_path: Path) -> None:
    markup = """
Статья 1. Предмет договора
1.1. Исполнитель обязуется оказать услуги по уборке помещений.
1.2. Услуги оказываются ежедневно.
**Статья 2**
**Цена договора**
2.1. Цена договора является твёрдой.
Раздел II
1. Общие требования к услугам
1.1. Требования указаны в техническом задании.
"""
    _, structure = structure_of(tmp_path, markup, root="Договор")
    assert labels(structure) == [
        (1, "Статья 1. Предмет договора"),
        (2, "1.1"),
        (2, "1.2"),
        (1, "Статья 2. Цена договора"),
        (2, "2.1"),
        (1, "Раздел II"),
        (2, "1. Общие требования к услугам"),
        (3, "1.1"),
    ]


def test_tabbed_numbering_from_word(tmp_path: Path) -> None:
    pages = [
        [
            PdfLine("1. ПРЕДМЕТ", bold=True),
            PdfLine("1.1."),
            PdfLine("Поставщик обязуется поставить Товар.", x=100, same_row=True),
            PdfLine("1.2."),
            PdfLine("Заказчик обязуется оплатить Товар.", x=100, same_row=True),
        ]
    ]
    doc = extract_document(build_pdf(PdfSpec(pages=pages), tmp_path / "doc.pdf"))
    assert "1.1. Поставщик обязуется поставить Товар." in doc.text
    structure = build_structure(doc, "Контракт")
    assert labels(structure) == [(1, "1. ПРЕДМЕТ"), (2, "1.1"), (2, "1.2")]


def test_docx_style_hints(tmp_path: Path) -> None:
    markup = """
Общие требования
Товар должен быть новым, не бывшим в употреблении.
Требования к упаковке
Упаковка должна обеспечивать сохранность Товара.
"""
    _, structure = structure_of(tmp_path, markup, root="ТЗ")
    assert labels(structure) == [(1, "Текст документа")]  # без стилей это обычный текст
    hints = {"Общие требования": 1, "Требования к упаковке": 2}
    _, structure = structure_of(tmp_path, markup, root="ТЗ", hints=hints)
    assert labels(structure) == [(1, "Общие требования"), (2, "Требования к упаковке")]


def test_docx_hints_with_word_numbering_and_wrapped_title(tmp_path: Path) -> None:
    markup = """
1. Требования к качеству, безопасности, упаковке, отгрузке, маркировке
и иным характеристикам поставляемого товара
Товар должен соответствовать требованиям технических регламентов.
2. Место поставки
Поставка осуществляется по адресу Заказчика.
"""
    hints = {
        "Требования к качеству, безопасности, упаковке, отгрузке, маркировке "
        "и иным характеристикам поставляемого товара": 1,
        "Место поставки": 1,
    }
    _, structure = structure_of(tmp_path, markup, root="ТЗ", hints=hints)
    assert labels(structure) == [
        (
            1,
            "1. Требования к качеству, безопасности, упаковке, отгрузке, маркировке "
            "и иным характеристикам поставляемого товара",
        ),
        (1, "2. Место поставки"),
    ]


def test_bold_subheading_inside_numbered_section(tmp_path: Path) -> None:
    markup = """
!! ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ
**1. Требования к товару**
**Требования к упаковке**
1.1. Упаковка должна обеспечивать сохранность Товара.
**2. Требования к поставке**
2.1. Поставка осуществляется силами Поставщика.
"""
    _, structure = structure_of(tmp_path, markup, root="ТЗ")
    assert structure.title == "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ"
    assert labels(structure) == [
        (1, "Вводная часть"),
        (1, "1. Требования к товару"),
        (2, "Требования к упаковке"),
        (3, "1.1"),
        (1, "2. Требования к поставке"),
        (2, "2.1"),
    ]


def test_attribution_line_above_title_is_not_an_appendix(tmp_path: Path) -> None:
    markup = """
Приложение № 3
к извещению об осуществлении закупки
!! ПРОЕКТ КОНТРАКТА
**1. ПРЕДМЕТ КОНТРАКТА**
1.1. Поставщик обязуется поставить Товар.
===
Приложение № 1
к Контракту
!! СПЕЦИФИКАЦИЯ
Бумага А4 — 100 пачек.
"""
    _, structure = structure_of(tmp_path, markup)
    assert structure.title == "ПРОЕКТ КОНТРАКТА"
    assert labels(structure) == [
        (1, "Вводная часть"),
        (1, "1. ПРЕДМЕТ КОНТРАКТА"),
        (2, "1.1"),
        (1, "Приложение № 1"),
        (2, "СПЕЦИФИКАЦИЯ"),
    ]


def test_unstructured_document(tmp_path: Path) -> None:
    doc, structure = structure_of(tmp_path, "Просто текст письма.\nВторой абзац письма.")
    assert labels(structure) == [(1, "Текст документа")]
    assert structure.nodes[0].char_end == len(doc.text)
    assert structure.title is None


@pytest.mark.parametrize(
    "line",
    [
        "10 (десяти) рабочих дней",  # номер без точки
        "1.5 % от цены контракта",  # процент
        "15.01.2026 года",  # дата
        "1) копия паспорта",  # подпункт-список
        "ООО «РОМАШКА»",  # прописными, но это реквизиты
        "ЗАКАЗЧИК:",  # подпись в реквизитах
    ],
)
def test_not_a_heading(tmp_path: Path, line: str) -> None:
    markup = f"""
**1. ОБЩИЕ ПОЛОЖЕНИЯ**
1.1. Текст пункта.
{line}
"""
    _, structure = structure_of(tmp_path, markup)
    assert labels(structure) == [(1, "1. ОБЩИЕ ПОЛОЖЕНИЯ"), (2, "1.1")]
