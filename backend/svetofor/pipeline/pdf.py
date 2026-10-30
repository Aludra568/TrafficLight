"""Тонкая обёртка над PyMuPDF.

Аннотации типов pymupdf неполные (конструктор Document без типов, итерация не описана),
поэтому документ отдаём как Any и не размазываем `type: ignore` по модулям конвейера.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pymupdf


def open_pdf(path: Path | None = None) -> Any:
    """Открыть PDF (или создать пустой документ, если путь не указан). Использовать в `with`."""
    opener = cast(Any, pymupdf.open)
    return opener(path) if path is not None else opener()
