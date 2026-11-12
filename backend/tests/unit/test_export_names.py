"""Тесты формирования безопасных имён файлов выгрузок портфелей (xlsx / docx)."""

from __future__ import annotations

import pytest

from svetofor.reports.export_names import (
    FALLBACK_TITLE,
    MAX_TITLE_LENGTH,
    clean_portfolio_title,
    portfolio_export_name,
)


def test_cyrillic_names() -> None:
    """Обычные русские названия портфелей формируют правильные имена файлов."""
    title = "Закупка оборудования 2026"
    assert portfolio_export_name(title, "xlsx") == "Чек-лист по «Закупка оборудования 2026».xlsx"
    assert portfolio_export_name(title, "docx") == "Отчёт по «Закупка оборудования 2026».docx"


def test_unsafe_windows_characters_replaced_with_underscore() -> None:
    r"""Запрещённые в Windows символы (<>:"/\|?*) заменяются на '_'."""
    title = 'План <1>: "Лот" / 2026 \\ Часть | ? *'
    expected = "План _1__ _Лот_ _ 2026 _ Часть _ _ _"
    assert clean_portfolio_title(title) == expected
    assert portfolio_export_name(title, "xlsx") == f"Чек-лист по «{expected}».xlsx"


def test_slashes_replaced_not_stripped_as_path() -> None:
    """Слэши заменяются на '_', а не отсекают часть названия как путь каталога."""
    title = "каталог/подкаталог\\файл"
    assert clean_portfolio_title(title) == "каталог_подкаталог_файл"
    assert portfolio_export_name(title, "docx") == "Отчёт по «каталог_подкаталог_файл».docx"


def test_control_characters_and_crlf_replaced_with_underscore() -> None:
    """Управляющие символы ASCII (0x00-0x1F, 0x7F) и переводы строк CRLF заменяются на '_'."""
    title = "Строка 1\r\nСтрока 2\t\x00\x1f\x7fКонец"
    expected = "Строка 1__Строка 2____Конец"
    assert clean_portfolio_title(title) == expected
    assert portfolio_export_name(title, "xlsx") == f"Чек-лист по «{expected}».xlsx"


def test_strip_spaces_and_dots_edges_only() -> None:
    """Точки и пробелы по краям удаляются, внутри названия — сохраняются."""
    title = " . . . Проект 1.0 от 12.11 . . . "
    expected = "Проект 1.0 от 12.11"
    assert clean_portfolio_title(title) == expected
    assert portfolio_export_name(title, "docx") == f"Отчёт по «{expected}».docx"


def test_fallback_to_default_when_empty_or_only_stripped_chars() -> None:
    """Пустое название, только пробелы или только точки дают fallback 'Портфель'."""
    assert clean_portfolio_title("") == FALLBACK_TITLE
    assert clean_portfolio_title("   ") == FALLBACK_TITLE
    assert clean_portfolio_title("...") == FALLBACK_TITLE
    assert clean_portfolio_title(" . . . ") == FALLBACK_TITLE
    assert clean_portfolio_title(None) == FALLBACK_TITLE

    assert portfolio_export_name("", "xlsx") == "Чек-лист по «Портфель».xlsx"
    assert portfolio_export_name("   ", "docx") == "Отчёт по «Портфель».docx"
    assert portfolio_export_name("...", "xlsx") == "Чек-лист по «Портфель».xlsx"


def test_long_title_truncated_to_120_codepoints() -> None:
    """Длинное название обрезается ровно до 120 кодовых точек Unicode."""
    long_title = "А" * 150
    assert len(clean_portfolio_title(long_title)) == MAX_TITLE_LENGTH
    assert clean_portfolio_title(long_title) == "А" * 120
    assert portfolio_export_name(long_title, "xlsx") == f"Чек-лист по «{'А' * 120}».xlsx"

    exact_120 = "Б" * 120
    assert clean_portfolio_title(exact_120) == exact_120


def test_astral_unicode_characters_and_length() -> None:
    """Символы astral Unicode (эмодзи, суррогатные пары) сохраняются и считаются как 1 codepoint."""
    title = "Портфель 🚀📁🦄"
    assert clean_portfolio_title(title) == "Портфель 🚀📁🦄"
    assert portfolio_export_name(title, "xlsx") == "Чек-лист по «Портфель 🚀📁🦄».xlsx"

    # 130 эмодзи обрезаются ровно до 120 кодовых точек
    astral_long = "🚀" * 130
    cleaned = clean_portfolio_title(astral_long)
    assert len(cleaned) == 120
    assert cleaned == "🚀" * 120
    assert portfolio_export_name(astral_long, "docx") == f"Отчёт по «{'🚀' * 120}».docx"


def test_allowed_extensions_only_xlsx_and_docx() -> None:
    """Разрешены только расширения xlsx и docx, другие вызывают ValueError."""
    title = "Тест"
    # Допустимые варианты (включая регистр и точку)
    assert portfolio_export_name(title, "xlsx") == "Чек-лист по «Тест».xlsx"
    assert portfolio_export_name(title, ".xlsx") == "Чек-лист по «Тест».xlsx"
    assert portfolio_export_name(title, "XLSX") == "Чек-лист по «Тест».xlsx"
    assert portfolio_export_name(title, "docx") == "Отчёт по «Тест».docx"
    assert portfolio_export_name(title, ".docx") == "Отчёт по «Тест».docx"
    assert portfolio_export_name(title, "DOCX") == "Отчёт по «Тест».docx"

    # Недопустимые варианты
    for bad_ext in ("pdf", "csv", "exe", "txt", ""):
        with pytest.raises(ValueError, match="Разрешены только 'xlsx' и 'docx'"):
            portfolio_export_name(title, bad_ext)


@pytest.mark.parametrize(
    ("title", "ext", "expected"),
    [
        (
            "Аукцион на поставку оргтехники",
            "xlsx",
            "Чек-лист по «Аукцион на поставку оргтехники».xlsx",
        ),
        (
            "Аукцион на поставку оргтехники",
            "docx",
            "Отчёт по «Аукцион на поставку оргтехники».docx",
        ),
        (
            "отдел/закупки/2026\\лот:1",
            "xlsx",
            "Чек-лист по «отдел_закупки_2026_лот_1».xlsx",
        ),
        (
            "Документ <1> *проект*?\r\nверсия|2",
            "docx",
            "Отчёт по «Документ _1_ _проект____версия_2».docx",
        ),
        (
            "Тендер 🏆 2026",
            "xlsx",
            "Чек-лист по «Тендер 🏆 2026».xlsx",
        ),
        (
            "   ...   ",
            "xlsx",
            "Чек-лист по «Портфель».xlsx",
        ),
        (
            "",
            "docx",
            "Отчёт по «Портфель».docx",
        ),
    ],
)
def test_server_browser_parity_vectors(title: str, ext: str, expected: str) -> None:
    """Векторы паритета между бэкендом и фронтендом выдают одинаковый результат."""
    assert portfolio_export_name(title, ext) == expected
