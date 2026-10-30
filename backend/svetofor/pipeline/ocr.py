"""Распознавание сканов.

Распознаются только страницы почти без текстового слоя: `--skip-text` оставляет остальные как есть,
`--pages` ограничивает работу найденными сканами. Текст добавляется невидимым слоем, поэтому
подсветка и просмотрщик работают со сканами так же, как с обычными PDF.

Страницы с нечитаемым текстовым слоем (шрифт без таблицы Unicode) тоже распознаются — с
`--force-ocr`: испорченный текст заменяется распознанным. Такой запуск растрирует выбранные
страницы, поэтому включается только когда такие страницы есть.

OCRmyPDF запускается отдельным процессом: падение или зависание распознавания не роняет воркер,
а документ остаётся в проверке с честной пометкой «страницы N не распознаны».
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from svetofor.core.tool_env import run_untrusted
from svetofor.pipeline.extract import pages_needing_ocr
from svetofor.pipeline.textlayer import text_quality

PAGE_TIMEOUT = 180  # с на страницу (ограничение Tesseract)

# Коды выхода OCRmyPDF → объяснение для пользователя
_EXIT_MESSAGES = {
    3: "Распознавание недоступно: на сервере не установлен Tesseract",
    8: "Файл защищён паролем — распознать сканы нельзя",
}


@dataclass(frozen=True)
class OcrOutcome:
    pdf_path: Path  # PDF с текстовым слоем; при ошибке — исходный
    pages: tuple[int, ...]  # страницы, которые отправлялись на распознавание
    error: str | None = None  # для пользователя; None — всё распознано
    garbage: tuple[int, ...] = ()  # из них — с нечитаемым текстовым слоем (а не сканы)

    @property
    def needed(self) -> bool:
        return bool(self.pages)


def page_ranges(pages: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] → «1-3,5,7-8» (формат `--pages` OCRmyPDF)."""
    ranges: list[str] = []
    ordered = sorted(set(pages))
    start = prev = ordered[0]
    for page in [*ordered[1:], None]:
        if page is not None and page == prev + 1:
            prev = page
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        if page is not None:
            start = prev = page
    return ",".join(ranges)


def ocr_command(
    src: Path, dest: Path, pages: list[int], *, languages: str, jobs: int, force: bool = False
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ocrmypdf",
        # force — заменить испорченный текстовый слой; иначе страницы с текстом не трогаем
        "--force-ocr" if force else "--skip-text",
        "--pages",
        page_ranges(pages),
        "--language",
        languages,
        "--jobs",
        str(jobs),
        "--deskew",
        "--rotate-pages",
        # очистка фона и пятен перед распознаванием (unpaper); итоговый PDF не меняется.
        # Без unpaper (локальный запуск) OCRmyPDF с этим флагом падал бы — тогда без него
        *(["--clean"] if shutil.which("unpaper") else []),
        "--output-type",
        "pdf",
        "--optimize",
        "0",
        # работаем с копией документа: подпись копии не важна, оригинал пакета не меняется
        "--invalidate-digital-signatures",
        "--tesseract-timeout",
        str(PAGE_TIMEOUT),
        "--quiet",
        str(src),
        str(dest),
    ]


def _pages_text(pages: tuple[int, ...]) -> str:
    return page_ranges(list(pages)).replace("-", "–").replace(",", ", ")


def ocr_if_needed(
    pdf: Path,
    dest: Path,
    *,
    min_chars: int,
    languages: str,
    jobs: int,
    timeout: int,
) -> OcrOutcome:
    scans, garbage = pages_needing_ocr(pdf, min_chars)
    pages = sorted({*scans, *garbage})
    if not pages:
        return OcrOutcome(pdf, ())
    found, bad = tuple(pages), tuple(garbage)
    command = ocr_command(pdf, dest, pages, languages=languages, jobs=jobs, force=bool(garbage))
    try:
        result = run_untrusted(command, timeout=timeout)  # без секретов: документ — чужой
    except subprocess.TimeoutExpired:
        message = f"Распознавание не уложилось во время: страницы {_pages_text(found)} без текста"
        return OcrOutcome(pdf, found, _with_garbage(message, bad), bad)
    if result.returncode != 0 or not dest.is_file():
        reason = _EXIT_MESSAGES.get(
            result.returncode, f"Не удалось распознать страницы {_pages_text(found)}"
        )
        return OcrOutcome(pdf, found, _with_garbage(reason, bad), bad)
    return OcrOutcome(dest, found, None, bad)


def _with_garbage(message: str, garbage: tuple[int, ...]) -> str:
    if not garbage:
        return message
    return (
        f"{message}. На страницах {_pages_text(garbage)} текст в файле нечитаемый "
        "(повреждённые шрифты) — проверка по ним ненадёжна"
    )


def ocr_text_quality(text: str) -> float | None:
    """Доля «похожих на слова» токенов распознанного текста, 0..1.

    Это оценка по тексту, а не уверенность Tesseract (OCRmyPDF её не отдаёт).

    Мусор распознавания («ш|г», «.,;», «Illl») снижает долю. None — текста слишком мало для оценки.
    """
    return text_quality(text)
