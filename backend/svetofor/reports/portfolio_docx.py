"""Сводный Word с результатом каждой закупки и ссылками на доказательства."""

from __future__ import annotations

import io
from urllib.parse import urlencode

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph

from svetofor.reports.docx_report import DISCLAIMER, xml_safe
from svetofor.reports.handling import HANDLING_TITLES
from svetofor.reports.xlsx import app_origin
from svetofor.schemas.portfolios import PortfolioOut

LIGHTS = {
    "red": "Критично",
    "yellow": "Внимание",
    "green": "Рисков не найдено",
    "gray": "Не оценено",
}
STATES = {"done": "Готово", "running": "В работе", "waiting": "Ожидает", "failed": "Ошибка"}


def _link(paragraph: Paragraph, text: str, url: str) -> None:
    link = OxmlElement("w:hyperlink")
    link.set(
        qn("r:id"), paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    )
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "503487")
    properties.append(color)
    run.append(properties)
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def render_portfolio_docx(data: PortfolioOut, base_url: str) -> bytes:
    base_url = app_origin(base_url)
    data = xml_safe(data)
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = section.right_margin = Cm(2)
    style = document.styles["Normal"]
    style.font.name, style.font.size = "Calibri", Pt(11)
    title = document.add_paragraph("Отчёт по портфелю закупок", "Title")
    for run in title.runs:
        run.font.color.rgb = RGBColor(0, 0, 0)
    document.add_paragraph(data.title)
    document.add_paragraph(
        f"Итог: {LIGHTS[data.light]}. "
        + (
            "Проверка завершена."
            if data.complete
            else "Предварительный результат: проверка продолжается."
        )
    )
    document.add_paragraph(
        f"Закупок: {data.total}. Готово: {data.done}. В работе: {data.running}. "
        f"Ожидают: {data.waiting}. С ошибкой: {data.failed}."
    )
    document.add_paragraph(
        "Ошибки и неоценённые закупки не считаются безопасными. "
        "Сохранённые замечания проверок с ошибкой включены; их анализ неполон. "
        "Статусы обработки не меняют риск. Ссылки требуют входа в приложение."
    )
    document.add_heading("Распределение закупок", 1)
    for light, count in data.distribution.items():
        document.add_paragraph(f"{LIGHTS[light]}: {count}")
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ("Закупка", "Статус", "Риск"), strict=True):
        cell.text = text
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for item in data.items:
        cells = table.add_row().cells
        cells[0].text = item.source_name
        cells[1].text = STATES[item.status] + (f": {item.error}" if item.error else "")
        cells[2].text = LIGHTS[item.light] if item.light else "Не оценено"
        if item.check_id:
            _link(
                cells[0].add_paragraph(), "Открыть проверку", f"{base_url}/checks/{item.check_id}"
            )
    document.add_heading("Повторяющиеся риски", 1)
    for risk in data.repeated_risks:
        document.add_paragraph(f"{risk.rule_code} — {risk.title}: {risk.procurements} закупки")
    if not data.repeated_risks:
        document.add_paragraph("Повторяющиеся риски среди готовых закупок не найдены.")
    document.add_heading("Замечания по критичности", 1)
    for finding in data.findings:
        block_start = len(document.paragraphs)
        paragraph = document.add_paragraph()
        paragraph.add_run(
            f"{LIGHTS[finding.severity]} · {finding.rule_code} · {finding.source_name}"
        ).bold = True
        document.add_paragraph(finding.title)
        document.add_paragraph(
            finding.quote or "Условие не найдено; подтверждающая цитата отсутствует."
        )
        document.add_paragraph(f"Обработка: {HANDLING_TITLES[finding.handling_status]}")
        params = {"finding": str(finding.id)}
        if finding.document_id and finding.page:
            params.update(document=str(finding.document_id), page=str(finding.page))
        _link(
            document.add_paragraph(),
            "Открыть замечание и источник",
            f"{base_url}/checks/{finding.check_id}?{urlencode(params)}",
        )
        for paragraph in document.paragraphs[block_start:-1]:
            paragraph.paragraph_format.keep_with_next = True
    document.add_paragraph(DISCLAIMER)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()
