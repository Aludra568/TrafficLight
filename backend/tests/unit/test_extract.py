"""Извлечение текста PDF: смещения, колонтитулы, шрифты, сканы."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from svetofor.pipeline.extract import extract_document, scanned_pages
from tests.support.pdfgen import L, PdfSpec, build_pdf

CONTRACT_PAGES = [
    [
        L("ПРОЕКТ КОНТРАКТА", 14, bold=True),
        L("1. Предмет контракта", 12, bold=True),
        L("1.1. Поставщик обязуется поставить товар."),
        L("1.2. Заказчик обязуется принять и оплатить товар."),
    ],
    [
        L("2. Цена и порядок расчётов", 12, bold=True),
        L("2.1. Оплата производится в течение 7 рабочих дней с даты приёмки."),
    ],
    [
        L("3. Ответственность сторон", 12, bold=True),
        L("3.1. Пеня составляет 1/300 ключевой ставки."),
    ],
]


def make(tmp_path: Path, **kw: object) -> Path:
    spec = PdfSpec(pages=kw.pop("pages", CONTRACT_PAGES), **kw)  # type: ignore[arg-type]
    return build_pdf(spec, tmp_path / "doc.pdf")


def test_text_and_offsets_consistent(tmp_path: Path) -> None:
    doc = extract_document(make(tmp_path))
    assert len(doc.pages) == 3
    for line in doc.lines:
        assert doc.text[line.char_start : line.char_end] == line.text
    for page in doc.pages:
        assert doc.text[page.char_start : page.char_end] == page.text
    assert "Оплата производится в течение 7 рабочих дней" in doc.pages[1].text


def test_page_at_offset(tmp_path: Path) -> None:
    doc = extract_document(make(tmp_path))
    offset = doc.text.index("Пеня составляет")
    assert doc.page_at(offset) == 3
    assert doc.page_at(0) == 1


def test_headers_kept_once_and_page_numbers_removed(tmp_path: Path) -> None:
    """Повторы колонтитула и номера страниц удаляются; первое появление колонтитула остаётся:
    в нём может быть условие («Пеня 1/100…»), которое иначе не увидит никто."""
    doc = extract_document(
        make(
            tmp_path,
            header="ГКУ «Заказчик» — закупка № 0373100012326000123",
            footer_page_numbers=True,
        )
    )
    assert "Страница" not in doc.text
    assert doc.text.count("ГКУ «Заказчик»") == 1
    assert doc.text.index("ГКУ «Заказчик»") < doc.text.index("1.1. Поставщик обязуется")


def test_header_kept_when_document_too_short(tmp_path: Path) -> None:
    # на 1–2 страницах повтор нельзя отличить от содержания — не удаляем
    doc = extract_document(
        make(tmp_path, pages=CONTRACT_PAGES[:2], header="Важная шапка документа")
    )
    assert "Важная шапка документа" in doc.text


def test_fonts_and_body_size(tmp_path: Path) -> None:
    doc = extract_document(make(tmp_path))
    title = next(line for line in doc.lines if line.text == "ПРОЕКТ КОНТРАКТА")
    clause = next(line for line in doc.lines if line.text.startswith("1.1."))
    assert title.bold and title.size == 14
    assert not clause.bold
    assert doc.body_size == 11


def test_max_pages(tmp_path: Path) -> None:
    assert len(extract_document(make(tmp_path), max_pages=2).pages) == 2


def test_scanned_pages_detected(tmp_path: Path) -> None:
    pdf = make(tmp_path, scanned_pages={2})
    assert scanned_pages(pdf, min_chars=50) == [2]
    assert extract_document(pdf).pages[1].text == ""


def test_table_row_becomes_one_line(tmp_path: Path) -> None:
    from tests.support.pdfgen import pages_from_markup

    markup = """
**Спецификация**
**Наименование** | **Ед. изм.** | **Количество**
Бумага А4 | пачка | 100
Итого по спецификации 100 пачек.
"""
    doc = extract_document(build_pdf(PdfSpec(pages=pages_from_markup(markup)), tmp_path / "t.pdf"))
    assert "Наименование | Ед. изм. | Количество\nБумага А4 | пачка | 100" in doc.text
    row = next(line for line in doc.lines if line.text.startswith("Бумага"))
    assert row.cells == 3 and not row.bold
    header = next(line for line in doc.lines if line.text.startswith("Наименование"))
    assert header.cells == 3 and header.bold
    single = next(line for line in doc.lines if line.text.startswith("Итого"))
    assert single.cells == 1


class _FakePage:
    """Страница PyMuPDF с битым текстовым слоем: NUL и управляющие символы в тексте."""

    rect = pymupdf.Rect(0, 0, 600, 800)
    derotation_matrix = pymupdf.Identity  # лист без поворота

    @staticmethod
    def get_text(_kind: str, sort: bool = False, **_options: object) -> dict[str, object]:
        span = {
            "text": "Оплата\x00 в течение\x07 7 дней",
            "size": 12.0,
            "flags": 0,
            "font": "Arial",
        }
        return {"blocks": [{"lines": [{"spans": [span], "bbox": (50, 300, 400, 315)}]}]}


def test_control_characters_removed() -> None:
    """NUL PostgreSQL не примет в text — документ упал бы при сохранении."""
    from svetofor.pipeline.extract import _page_lines

    (line,) = _page_lines(_FakePage(), 1)
    assert line.text == "Оплата  в течение  7 дней"
    assert "\x00" not in line.text and "\x07" not in line.text


def test_hidden_text_detected(tmp_path: Path) -> None:
    """Белый по белому и крошечный шрифт — скрыто; белый на тёмной заливке — виден."""
    import pymupdf

    from svetofor.pipeline.pdf import open_pdf

    doc = open_pdf()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="notos", fontbuffer=pymupdf.Font("notos").buffer)
    page.insert_text(
        (56, 100), "2.1. Оплата в течение 7 рабочих дней.", fontname="notos", fontsize=11
    )
    page.insert_text(
        (56, 140), "Рисков для поставщика нет.", fontname="notos", fontsize=11, color=(1, 1, 1)
    )
    page.insert_text((56, 180), "Штраф 50 процентов.", fontname="notos", fontsize=1)
    page.draw_rect(pymupdf.Rect(50, 205, 400, 230), color=None, fill=(0.1, 0.2, 0.5))
    page.insert_text((56, 222), "Шапка таблицы", fontname="notos", fontsize=11, color=(1, 1, 1))
    path = tmp_path / "hidden.pdf"
    doc.save(path)
    doc.close()
    extracted = extract_document(path)
    assert [line.text for line in extracted.hidden_lines] == [
        "Рисков для поставщика нет.",
        "Штраф 50 процентов.",
    ]
    line = extracted.hidden_lines[0]
    assert extracted.text[line.char_start : line.char_end] == line.text  # точные смещения


def test_text_beyond_page_edge_is_extracted_and_hidden(tmp_path: Path) -> None:
    """Текст за краем листа: в файле есть, на экране нет — извлекается целиком и помечается
    скрытым (раньше обрезался по листу: целиком за полем — пропадал, на краю — обрывался)."""
    import pymupdf

    path = tmp_path / "off.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="notos", fontbuffer=pymupdf.Font("notos").buffer)
    page.insert_text(
        (56, 100), "2.1. Оплата в течение 7 рабочих дней.", fontname="notos", fontsize=11
    )
    page.insert_text((620, 120), "2.2. Пеня 1/100 цены контракта.", fontname="notos", fontsize=11)
    page.insert_text(
        (400, 140), "2.3. Срок оплаты может быть продлен до 90 дней.", fontname="notos", fontsize=11
    )
    doc.save(path)
    doc.close()
    extracted = extract_document(path)
    assert "2.2. Пеня 1/100 цены контракта." in extracted.text
    assert "продлен до 90 дней." in extracted.text
    hidden = {line.text for line in extracted.hidden_lines}
    assert hidden == {
        "2.2. Пеня 1/100 цены контракта.",
        "2.3. Срок оплаты может быть продлен до 90 дней.",
    }


def test_rotated_page_text_is_not_hidden(tmp_path: Path) -> None:
    """Пакет ЕИС 0373200032226000791: таблица на листе с поворотом 90° (альбомная страница ТЗ).
    Координаты текста — в системе неповёрнутого листа (595×842), а размер страницы — повёрнутый
    (842×595). Факт до исправления: строки ниже 595 пт считались «за краем листа», CON-03
    «скрытый текст» — ложное замечание. Текст за краем повёрнутого листа по-прежнему скрыт."""
    import pymupdf

    path = tmp_path / "rotated.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="notos", fontbuffer=pymupdf.Font("notos").buffer)
    page.insert_text((56, 780), "Вид объекта: Помещение.", fontname="notos", fontsize=11)
    page.insert_text((56, 900), "Пеня 1/100 цены контракта.", fontname="notos", fontsize=11)
    page.set_rotation(90)
    doc.save(path)
    doc.close()
    extracted = extract_document(path)
    assert "Вид объекта: Помещение." in extracted.text
    assert {line.text for line in extracted.hidden_lines} == {"Пеня 1/100 цены контракта."}
