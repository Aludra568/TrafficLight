"""Замер точности OCR (критерий приёмки 4): метрика и сквозной прогон на скане."""

from __future__ import annotations

from pathlib import Path

import pytest

from svetofor.eval.ocr_accuracy import TARGET, accuracy, measure, normalize
from tests.support.pdfgen import PdfSpec, build_pdf, pages_from_markup


def test_spaces_and_line_breaks_are_not_errors() -> None:
    assert accuracy("Поставщик  обязуется\nпоставить", "Поставщик обязуется поставить") == 1.0
    assert normalize("подпи-\nсания") == "подписания"


def test_accuracy_is_one_minus_character_error_rate() -> None:
    assert accuracy("абвгде", "абвxде") == pytest.approx(5 / 6)
    assert accuracy("абв", "") == 0.0
    assert accuracy("", "что угодно") == 1.0


CONTRACT_PAGE = """!! ПРОЕКТ КОНТРАКТА
1. Поставщик обязуется поставить товар в количестве 500 пачек в течение 15 рабочих дней.
2. Оплата производится в течение 7 рабочих дней с даты подписания документа о приемке.
3. Приемка товара осуществляется в течение 5 рабочих дней с даты поставки товара.
4. Обеспечение исполнения контракта устанавливается в размере 5 процентов цены контракта.
5. Пеня начисляется в размере одной трехсотой ключевой ставки Центрального банка.
6. Штрафы начисляются в соответствии с постановлением Правительства № 1042.
"""


@pytest.mark.docker("tesseract")
@pytest.mark.slow
def test_quality_scans_reach_target(tmp_path: Path) -> None:
    """Страница договора как скан 300 dpi (чистый, с наклоном, со сжатием) — ≥ 95 %."""
    source = build_pdf(PdfSpec(pages=pages_from_markup(CONTRACT_PAGE)), tmp_path / "contract.pdf")
    scores = measure(source, languages="rus+eng", jobs=1)
    assert {s.variant for s in scores} == {"clean", "skew", "jpeg", "office"}
    quality = [s for s in scores if s.variant != "office"]
    assert min(s.accuracy for s in quality) >= TARGET, scores
