"""Файлы отчёта: Word — сразу, PDF — из того же Word через LibreOffice (задача Celery)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from svetofor.db.models import Check
from svetofor.pipeline.convert import ConvertError, office_to_pdf
from svetofor.pipeline.storage import PackagePaths
from svetofor.reports.docx_report import render_report_docx
from svetofor.reports.report import build_report


def report_paths(check: Check) -> tuple[Path, Path, Path]:
    """(DOCX, PDF, файл с причиной неудачи PDF) в папке отчётов пакета."""
    folder = PackagePaths(Path(check.package.storage_path)).reports
    return (
        folder / f"{check.id}.docx",
        folder / f"{check.id}.pdf",
        folder / f"{check.id}.pdf.error",
    )


def download_name(check: Check, extension: str) -> str:
    return f"Светофор — отчёт — {check.package.title}{extension}"


def write_docx(db: Session, check: Check) -> Path:
    docx_path, _, _ = report_paths(check)
    return render_report_docx(build_report(db, check), docx_path)


def write_pdf(db: Session, check: Check, soffice: str = "soffice") -> Path | None:
    """PDF отчёта. Неудача — None и понятная причина в файле рядом (её покажет API)."""
    docx_path = write_docx(db, check)
    _, pdf_path, error_path = report_paths(check)
    error_path.unlink(missing_ok=True)
    try:
        office_to_pdf(docx_path, pdf_path, soffice=soffice, work_dir=docx_path.parent)
    except ConvertError as exc:
        error_path.write_text(f"Не удалось подготовить PDF: {exc}", encoding="utf-8")
        return None
    return pdf_path
