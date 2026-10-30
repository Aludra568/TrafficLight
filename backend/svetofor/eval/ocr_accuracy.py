"""Точность OCR на «качественных сканах» — критерий приёмки 4 (≥ 95 %).

Эталон — PDF с текстовым слоем (настоящие документы закупки). Каждую страницу превращаем в скан:
картинка без текста, 300 dpi, в трёх видах — чистый, с наклоном 1° и с JPEG-сжатием (так выглядят
сканы с офисного МФУ). Скан проходит тот же конвейер, что в продукте (`ocr_if_needed`: OCRmyPDF,
Tesseract rus+eng, выравнивание, поворот, очистка), и распознанный текст сравнивается с эталоном.

Точность — посимвольная: 1 − расстояние Левенштейна / длина эталона, после схлопывания пробелов
(переносы строк и двойные пробелы — не ошибка распознавания). Нужен Tesseract — запуск в
Docker-образе: `docker compose run --rm api python -m svetofor.eval ocr --source /app/samples/...`.
"""

from __future__ import annotations

import io
import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, cast

import pymupdf
from PIL import Image
from rapidfuzz.distance import Levenshtein

from svetofor.pipeline.ocr import ocr_if_needed

DPI = 300
VARIANTS = ("clean", "skew", "jpeg", "office")
# «office» — плохой скан с офисного МФУ: 200 dpi, наклон 1,5°, шум, сильное сжатие. В критерий
# (≥ 95 % «для качественных сканов») не входит — показывает, где кончается запас
QUALITY_VARIANTS = ("clean", "skew", "jpeg")
TARGET = 0.95
# у pymupdf нет аннотаций типов: работаем через Any, как в synthetic.py
_pdf = cast(Any, pymupdf)


@dataclass(frozen=True)
class PageScore:
    source: str
    page: int
    variant: str
    accuracy: float


def normalize(text: str) -> str:
    """Пробелы и переносы строк не считаются: сравниваем последовательность слов."""
    text = text.replace("­", "").replace("-\n", "")
    return re.sub(r"\s+", " ", text).strip()


def accuracy(reference: str, recognized: str) -> float:
    ref, got = normalize(reference), normalize(recognized)
    if not ref:
        return 1.0
    return max(0.0, 1 - Levenshtein.distance(ref, got) / len(ref))


def _image(page: Any, variant: str) -> bytes:
    pix = page.get_pixmap(dpi=200 if variant == "office" else DPI, colorspace=_pdf.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if variant in ("skew", "office"):
        angle = 1.5 if variant == "office" else 1.0
        image = image.rotate(angle, expand=True, fillcolor=255, resample=Image.Resampling.BICUBIC)
    if variant == "office":
        noise = Image.effect_noise(image.size, 40).point(lambda v: 255 if v > 250 else 0)
        image = Image.composite(Image.new("L", image.size, 120), image, noise)
    buffer = io.BytesIO()
    if variant in ("jpeg", "office"):
        image.save(buffer, format="JPEG", quality=60 if variant == "jpeg" else 35)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def scan_of(page: Any, variant: str, dest: Path) -> Path:
    """Страница как скан: одна картинка на странице того же размера, текстового слоя нет."""
    with _pdf.open() as out:
        sheet = out.new_page(width=page.rect.width, height=page.rect.height)
        sheet.insert_image(sheet.rect, stream=_image(page, variant))
        out.save(dest)
    return dest


def source_pdfs(source: Path, work: Path) -> Iterator[Path]:
    """PDF из папки, одного файла или ZIP-архива (демо-пакет)."""
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for number, info in enumerate(archive.infolist()):
                if info.filename.lower().endswith(".pdf"):
                    target = work / f"src{number}.pdf"
                    target.write_bytes(archive.read(info))
                    yield target
    elif source.is_dir():
        yield from sorted(source.glob("*.pdf"))
    else:
        yield source


def _page_scores(
    pdf: Path, index: int, page: Any, work: Path, languages: str, jobs: int
) -> list[PageScore]:
    reference = page.get_text()
    if len(normalize(reference)) < 200:
        return []  # почти пустая страница — точность ничего не скажет
    scores = []
    for variant in VARIANTS:
        name = f"{pdf.stem}-{index}-{variant}"
        outcome = ocr_if_needed(
            scan_of(page, variant, work / f"scan-{name}.pdf"),
            work / f"ocr-{name}.pdf",
            min_chars=50,
            languages=languages,
            jobs=jobs,
            timeout=300,
        )
        with _pdf.open(outcome.pdf_path) as recognized:
            text = recognized[0].get_text()
        scores.append(PageScore(pdf.name, index + 1, variant, accuracy(reference, text)))
    return scores


def measure(source: Path, *, languages: str = "rus+eng", jobs: int = 2) -> list[PageScore]:
    scores: list[PageScore] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for pdf in source_pdfs(source, work):
            with _pdf.open(pdf) as doc:
                for index, page in enumerate(doc):
                    scores.extend(_page_scores(pdf, index, page, work, languages, jobs))
    return scores


def quality_mean(scores: list[PageScore]) -> float:
    """Средняя по качественным сканам — то, что меряет критерий приёмки."""
    values = [s.accuracy for s in scores if s.variant in QUALITY_VARIANTS]
    return mean(values) if values else 0.0


def report(scores: list[PageScore]) -> str:
    if not scores:
        return "Нет страниц с текстом для замера."
    lines = [f"Страниц × вариантов: {len(scores)}"]
    for variant in VARIANTS:
        values = [s.accuracy for s in scores if s.variant == variant]
        if values:
            average, worst = mean(values), min(values)
            count = len(values)
            lines.append(f"{variant:6} средняя {average:.2%}, минимум {worst:.2%} ({count} стр.)")
    total = quality_mean(scores)
    verdict = "выполнен" if total >= TARGET else "НЕ выполнен"
    lines.append(f"Качественные сканы: {total:.2%} — критерий ≥ {TARGET:.0%} {verdict}")
    return "\n".join(lines)
