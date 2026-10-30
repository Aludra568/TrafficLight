"""Распознавание: какие страницы, команда, обработка сбоев; настоящий OCR — в Docker."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from svetofor.pipeline import ocr
from svetofor.pipeline.extract import extract_document
from svetofor.pipeline.ocr import ocr_command, ocr_if_needed, ocr_text_quality, page_ranges
from tests.support.pdfgen import L, PdfSpec, build_pdf

OPTIONS: dict[str, Any] = {"min_chars": 50, "languages": "rus+eng", "jobs": 2, "timeout": 60}
TEXT_LINE = L("Поставщик обязуется поставить товар в течение десяти рабочих дней с даты заявки.")


@pytest.mark.parametrize(
    ("pages", "expected"),
    [([1], "1"), ([1, 2, 3], "1-3"), ([1, 2, 3, 5, 7, 8], "1-3,5,7-8"), ([4, 2, 2], "2,4")],
)
def test_page_ranges(pages: list[int], expected: str) -> None:
    assert page_ranges(pages) == expected


def test_command(tmp_path: Path) -> None:
    command = ocr_command(
        tmp_path / "a.pdf", tmp_path / "b.pdf", [2, 3], languages="rus+eng", jobs=4
    )
    assert command[1:3] == ["-m", "ocrmypdf"]
    joined = " ".join(command)
    for flag in ("--skip-text", "--pages 2-3", "--language rus+eng", "--jobs 4", "--deskew"):
        assert flag in joined
    assert command[-2:] == [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")]


@pytest.mark.parametrize(("unpaper", "cleaned"), [("/usr/bin/unpaper", True), (None, False)])
def test_clean_only_with_unpaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unpaper: str | None, cleaned: bool
) -> None:
    """Очистка фона повышает точность OCR, но без unpaper OCRmyPDF с --clean не запустится."""
    monkeypatch.setattr("svetofor.pipeline.ocr.shutil.which", lambda _: unpaper)
    command = ocr_command(tmp_path / "a.pdf", tmp_path / "b.pdf", [1], languages="rus", jobs=1)
    assert ("--clean" in command) is cleaned


def forbid_subprocess(*_: object, **__: object) -> None:
    raise AssertionError("распознавание не должно запускаться")


def test_no_scans_no_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = build_pdf(PdfSpec(pages=[[TEXT_LINE]]), tmp_path / "doc.pdf")
    monkeypatch.setattr(ocr.subprocess, "run", forbid_subprocess)
    outcome = ocr_if_needed(pdf, tmp_path / "ocr.pdf", **OPTIONS)
    assert outcome.pdf_path == pdf and not outcome.needed and outcome.error is None


def scanned(tmp_path: Path) -> Path:
    return build_pdf(
        PdfSpec(pages=[[TEXT_LINE], [], [TEXT_LINE]], scanned_pages={2}), tmp_path / "doc.pdf"
    )


def fake_run(returncode: int, write: bool = True) -> Any:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if write:
            Path(command[-1]).write_bytes(Path(command[-2]).read_bytes())
        return subprocess.CompletedProcess(command, returncode)

    return run


def test_scans_recognized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.subprocess, "run", fake_run(0))
    outcome = ocr_if_needed(scanned(tmp_path), tmp_path / "ocr.pdf", **OPTIONS)
    assert outcome.pages == (2,) and outcome.error is None
    assert outcome.pdf_path == tmp_path / "ocr.pdf"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (8, "защищён паролем"),
        (3, "не установлен Tesseract"),
        (15, "Не удалось распознать страницы 2"),
    ],
)
def test_ocr_failure_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    pdf = scanned(tmp_path)
    monkeypatch.setattr(ocr.subprocess, "run", fake_run(code, write=False))
    outcome = ocr_if_needed(pdf, tmp_path / "ocr.pdf", **OPTIONS)
    assert outcome.pdf_path == pdf and outcome.pages == (2,)
    assert outcome.error is not None and message in outcome.error


def test_ocr_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(command: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ocr.subprocess, "run", slow)
    outcome = ocr_if_needed(scanned(tmp_path), tmp_path / "ocr.pdf", **OPTIONS)
    assert outcome.error is not None and "не уложилось во время" in outcome.error


def test_text_quality() -> None:
    clean = "Поставщик обязуется поставить товар в течение 10 рабочих дней с даты заявки."
    garbage = "ш|г .,; Illl ~~ ‚‚ ф1ж }{ ===== ||| ¬¬ ±±"
    assert ocr_text_quality(clean) == 1.0
    quality = ocr_text_quality(garbage)
    assert quality is not None and quality < 0.3
    assert ocr_text_quality("два слова") is None


@pytest.mark.docker("tesseract")
@pytest.mark.slow
def test_real_ocr_on_rendered_scan(tmp_path: Path) -> None:
    source = build_pdf(
        PdfSpec(pages=[[L("Поставщик обязуется поставить товар", size=16)]]), tmp_path / "t.pdf"
    )
    scan = tmp_path / "scan.pdf"
    with pymupdf.open(source) as doc, pymupdf.open() as out:
        pix = doc[0].get_pixmap(dpi=300)
        page = out.new_page(width=doc[0].rect.width, height=doc[0].rect.height)
        page.insert_image(page.rect, pixmap=pix)
        out.save(scan)
    outcome = ocr_if_needed(scan, tmp_path / "ocr.pdf", **{**OPTIONS, "timeout": 300})
    assert outcome.error is None and outcome.pages == (1,)
    assert "Поставщик" in extract_document(outcome.pdf_path).text


def test_ocr_runs_without_app_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OCRmyPDF и Tesseract тоже читают чужой документ — секреты приложения им не передаются."""
    import subprocess

    from svetofor.pipeline import ocr

    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    monkeypatch.setattr(ocr, "pages_needing_ocr", lambda *_: ([1], []))
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ocr.ocr_if_needed(pdf, tmp_path / "out.pdf", min_chars=50, languages="rus", jobs=1, timeout=5)
    env = seen["env"]
    assert isinstance(env, dict) and "LLM_API_KEY" not in env
