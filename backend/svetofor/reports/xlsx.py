"""Рабочий XLSX: текст документов никогда не исполняется как формула."""

from __future__ import annotations

import io
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from svetofor.db.enums import Severity
from svetofor.reports.docx_report import xml_safe
from svetofor.reports.handling import HANDLING_TITLES
from svetofor.schemas.checks import ReportOut
from svetofor.schemas.portfolios import PortfolioOut

HEADERS = (
    "Закупка",
    "ID замечания",
    "Правило",
    "Категория",
    "Уровень риска",
    "Замечание",
    "Цитата",
    "Документ",
    "Пункт или раздел",
    "Страница",
    "Рекомендация",
    "Статус обработки",
    "Изменён",
    "Отчёт и замечание",
    "Документ и страница",
)
SEVERITIES = {"red": "Критично", "yellow": "Внимание"}
LIGHT_TITLES = {**SEVERITIES, "green": "Рисков не найдено", "gray": "Не оценено"}
FILLS = {"red": "FCE4E7", "yellow": "FFF2CC"}


def app_origin(base_url: str) -> str:
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError(
            "PUBLIC_APP_URL должен быть адресом приложения http/https без учётных данных"
        )
    if parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("PUBLIC_APP_URL должен содержать только адрес приложения без пути")
    return base_url.rstrip("/")


def text_cell(cell: Cell, value: str) -> None:
    clean = xml_safe(value)
    if len(clean) > 32767:
        clean = clean[:32680] + "\n[Текст длиннее лимита Excel. Полностью — по ссылке на отчёт.]"
    cell.value = clean
    cell.data_type = "s"
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def _style_sheet(sheet: Worksheet, widths: list[int]) -> None:
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 32
    for cell in sheet[1]:
        cell.font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="2C303C")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width


def _rows(sheet: Worksheet, reports: list[ReportOut], base_url: str) -> None:
    ordered = sorted(
        [(report, finding) for report in reports for finding in report.findings],
        key=lambda pair: (
            pair[1].severity != "red",
            pair[1].rule_code,
            pair[0].package.title,
            str(pair[1].id),
        ),
    )
    for report, finding in ordered:
        docs = {d.id: d.filename for d in report.documents}
        evidence = finding.evidence
        quote = "\n\n".join(e.quote for e in evidence) or finding.quote or "Условие не найдено"
        docnames = "; ".join(dict.fromkeys(docs.get(e.document_id, "Документ") for e in evidence))
        docnames = docnames or (
            docs.get(finding.document_id, "Источник не определён")
            if finding.document_id
            else "Источник не определён"
        )
        sections = "; ".join(e.section_path for e in evidence) or finding.section_path or ""
        page = finding.page_from or (evidence[0].page_from if evidence else None)
        doc = finding.document_id or (evidence[0].document_id if evidence else None)
        link = f"{base_url}/checks/{report.check.id}?{urlencode({'finding': str(finding.id)})}"
        document_link = ""
        if doc is not None and page is not None:
            document_link = link + "&" + urlencode({"document": str(doc), "page": page})
        values = [
            report.package.title,
            str(finding.id),
            finding.rule_code,
            finding.category_title,
            SEVERITIES[finding.severity],
            finding.title,
            quote,
            docnames,
            sections,
            str(page) if page else "",
            finding.recommendation,
            HANDLING_TITLES[finding.handling_status],
            finding.handling_updated_at.isoformat() if finding.handling_updated_at else "",
            "Открыть замечание",
            "Открыть документ" if document_link else "Нет подтверждённого места",
        ]
        row = sheet.max_row + 1
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row, col)
            text_cell(cell, value)
            cell.font = Font(name="Calibri", size=11)
        for col, target in ((14, link), (15, document_link)):
            if target:
                sheet.cell(row, col).hyperlink = target
                sheet.cell(row, col).font = Font(name="Calibri", color="503487", underline="single")
        sheet.cell(row, 5).fill = PatternFill("solid", fgColor=FILLS[finding.severity])
        sheet.row_dimensions[row].height = 72


def render_xlsx(
    reports: list[ReportOut],
    title: str,
    base_url: str,
    portfolio: PortfolioOut | None = None,
) -> bytes:
    base_url = app_origin(base_url)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Замечания"
    sheet.append(HEADERS)
    _rows(sheet, reports, base_url)
    _style_sheet(sheet, [35, 38, 14, 26, 16, 42, 72, 35, 32, 12, 60, 23, 28, 25, 26])
    summary = workbook.create_sheet("Сводка")
    summary.append(["Показатель", "Значение", "Критично", "Внимание", "Отработано", "Неактуально"])
    findings = [f for report in reports for f in report.findings]
    info: list[tuple[str, str | int]] = [
        ("Название", title),
        ("Снимок на", datetime.now(UTC).isoformat()),
        ("Закупок", portfolio.total if portfolio else len(reports)),
        ("Замечаний", len(findings)),
        ("Отработано", sum(f.handling_status == "resolved" for f in findings)),
        ("Неактуально", sum(f.handling_status == "not_applicable" for f in findings)),
        ("Примечание", "Статус обработки не меняет риск. Ссылки требуют входа в приложение."),
    ]
    if portfolio is not None:
        info.extend(
            (name, value)
            for name, value in (
                ("Готово", portfolio.done),
                ("В работе", portfolio.running),
                ("Ожидает", portfolio.waiting),
                ("С ошибкой", portfolio.failed),
                ("Итог", LIGHT_TITLES[portfolio.light]),
            )
        )
        for light, value in portfolio.distribution.items():
            info.append((f"Закупки: {LIGHT_TITLES[light]}", value))
    for name, metric in info:
        summary.append([name, metric])
    summary.append(["Категория", "Всего", "Критично", "Внимание", "Отработано", "Неактуально"])
    for category in sorted({f.category_title for f in findings}):
        own = [f for f in findings if f.category_title == category]
        amounts = Counter(f.severity for f in own)
        summary.append(
            [
                category,
                len(own),
                amounts[Severity.RED],
                amounts[Severity.YELLOW],
                sum(f.handling_status == "resolved" for f in own),
                sum(f.handling_status == "not_applicable" for f in own),
            ]
        )
    for row in summary:
        for cell in row:
            if isinstance(cell.value, str):
                text_cell(cell, cell.value)
    _style_sheet(summary, [42, 65, 18, 18, 18, 18])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
