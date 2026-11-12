"""Формирование безопасных имён файлов для выгрузок портфелей (Excel и Word)."""

from __future__ import annotations

import re

FALLBACK_TITLE = "Портфель"
MAX_TITLE_LENGTH = 120

# Недопустимые в Windows символы (<>:"/\|?*) и управляющие символы (ASCII 0x00-0x1F, 0x7F)
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def clean_portfolio_title(title: str | None) -> str:
    """Очищает название портфеля для безопасного использования в имени файла на диске.

    - Заменяет недопустимые символы Windows и управляющие символы (включая CRLF) на '_'
    - Слэши заменяются на '_', часть названия не отбрасывается как путь
    - Удаляет пробелы и точки по краям
    - Ограничивает длину 120 Unicode-символами (кодовыми точками)
    - При пустом результате возвращает 'Портфель'
    """
    if not title:
        return FALLBACK_TITLE

    # Заменяем запрещённые символы и слэши на '_'
    cleaned = _INVALID_CHARS_RE.sub("_", title).strip(" .")

    # Ограничение 120 Unicode codepoints после очистки
    cleaned = cleaned[:MAX_TITLE_LENGTH]

    return cleaned or FALLBACK_TITLE


def portfolio_export_name(title: str, extension: str) -> str:
    """Возвращает имя файла выгрузки портфеля по шаблону:

    - 'xlsx': «Чек-лист по «{НАЗВАНИЕ}».xlsx»
    - 'docx': «Отчёт по «{НАЗВАНИЕ}».docx»

    Разрешены только расширения 'xlsx' и 'docx'.
    """
    ext = extension.strip().lower().removeprefix(".")
    if ext == "xlsx":
        prefix = "Чек-лист по"
    elif ext == "docx":
        prefix = "Отчёт по"
    else:
        raise ValueError(
            f"Недопустимое расширение выгрузки: {extension!r}. Разрешены только 'xlsx' и 'docx'."
        )

    safe_title = clean_portfolio_title(title)
    return f"{prefix} «{safe_title}».{ext}"
