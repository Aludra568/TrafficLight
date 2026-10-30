"""Снимок конвейера для воспроизводимости.

«Почему 3 октября эта закупка была красной?» — версии правил, промптов и модели записываются и
так; здесь — всё остальное, от чего зависит текст и вердикт: версии OCR, LibreOffice, разборщика
PDF, модель эмбеддингов, бюджеты контекста и пороги. Версии внешних программ узнаются один раз
на процесс.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from importlib import metadata
from typing import Any

from svetofor import __version__
from svetofor.core.config import Settings
from svetofor.rules.aggregate import MIN_COVERAGE
from svetofor.rules.verify import MAX_ELLIPSIS_GAP

_PACKAGES = ("pymupdf", "ocrmypdf", "rapidfuzz", "lxml", "openai")


def _package(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _program(command: list[str]) -> str | None:
    path = shutil.which(command[0])
    if path is None:
        return None
    try:
        result = subprocess.run(
            [path, *command[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
    return output.splitlines()[0][:120] if output else None


@lru_cache(maxsize=1)
def tool_versions() -> dict[str, str | None]:
    return {
        **{name: _package(name) for name in _PACKAGES},
        "tesseract": _program(["tesseract", "--version"]),
        "libreoffice": _program(["soffice", "--version"]),
    }


def pipeline_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "app": __version__,
        "commit": os.environ.get("GIT_COMMIT") or None,
        "tools": tool_versions(),
        "embed_model": settings.embed_model if settings.embed_url else None,
        "llm_context_tokens": settings.llm_context_tokens,
        "second_opinion": settings.llm_second_opinion,
        "green_sweep": settings.green_sweep,
        "quote_min_score": settings.quote_min_score,
        "min_coverage": MIN_COVERAGE,
        "max_ellipsis_gap": MAX_ELLIPSIS_GAP,
        "ocr_languages": settings.ocr_languages,
        "chunk_max_tokens": settings.chunk_max_tokens,
    }
