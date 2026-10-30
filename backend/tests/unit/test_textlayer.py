"""Испорченный текстовый слой: «кракозябры» cp1251 чинятся на месте, мусор уходит в OCR."""

from __future__ import annotations

from pathlib import Path

from svetofor.pipeline.extract import extract_document, pages_needing_ocr
from svetofor.pipeline.ocr import ocr_command
from svetofor.pipeline.textlayer import is_garbage, is_mojibake, repair_mojibake, text_quality
from tests.support.pdfgen import L, PdfSpec, build_pdf

GOOD = (
    "2.1. Оплата производится в течение 7 рабочих дней с даты подписания документа о приёмке. "
    "Поставщик обязуется поставить товар в количестве 500 пачек в срок не более 10 дней."
)
MOJIBAKE = GOOD.encode("cp1251").decode("latin-1")  # «Îïëàòà ïðîèçâîäèòñÿ…»
GARBAGE = " ".join(["§¤¥", "¦¨©", "±²³", "¶·¸", "¼½¾", "¿×÷"] * 6)


def test_mojibake_repaired_char_for_char() -> None:
    assert is_mojibake(MOJIBAKE) and not is_mojibake(GOOD)
    repaired = repair_mojibake(MOJIBAKE)
    assert repaired == GOOD and len(repaired) == len(MOJIBAKE)  # смещения не сдвигаются


def test_garbage_vs_real_text() -> None:
    assert is_garbage(GARBAGE)
    assert not is_garbage(GOOD * 3)
    assert not is_garbage("Схема 1")  # слишком мало текста, чтобы судить
    assert text_quality(GOOD) is not None and text_quality(GOOD) > 0.9  # type: ignore[operator]


def test_mojibake_pdf_page_read_as_russian(tmp_path: Path) -> None:
    pdf = build_pdf(PdfSpec(pages=[[L(MOJIBAKE[:90]), L(MOJIBAKE[90:])]]), tmp_path / "m.pdf")
    text = extract_document(pdf).text
    assert "Оплата производится в течение 7 рабочих дней" in text
    assert pages_needing_ocr(pdf) == ([], [])  # починили без распознавания


def test_garbage_page_goes_to_forced_ocr(tmp_path: Path) -> None:
    pages = [[L("Проект контракта. " + GOOD)], [L(GARBAGE[:100]), L(GARBAGE[100:])]]
    pdf = build_pdf(PdfSpec(pages=pages), tmp_path / "g.pdf")
    assert pages_needing_ocr(pdf) == ([], [2])
    forced = ocr_command(pdf, tmp_path / "o.pdf", [2], languages="rus", jobs=1, force=True)
    assert "--force-ocr" in forced and "--skip-text" not in forced
    normal = ocr_command(pdf, tmp_path / "o.pdf", [2], languages="rus", jobs=1)
    assert "--skip-text" in normal and "--force-ocr" not in normal
