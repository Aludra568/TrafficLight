"""Неподтверждённые исправления Word (режим рецензирования) — принимаются до конвертации.

LibreOffice печатает документ с исправлениями так, как их показывает: удалённый текст остаётся
(зачёркнутым), вставленный — рядом. В тексте PDF оказываются оба варианта: «срок оплаты 30 15
дней» — и факт берётся не тот. Проверяем редакцию «все исправления приняты» — так документ
прочитает и суд — и честно предупреждаем, что исправления были.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PARTS = re.compile(r"^word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
# маркеры «знак абзаца / строка таблицы вставлены или удалены» — внутри свойств
_PROPERTY_PARENTS = frozenset({f"{{{W}}}rPr", f"{{{W}}}trPr"})
_MARKERS = tuple(
    f"{{{W}}}{name}"
    for name in (
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "tblPrExChange",
        "trPrChange",
        "tcPrChange",
        "tblGridChange",
        "numberingChange",
        "cellIns",
        "cellDel",
        "cellMerge",
    )
)


def _remove(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def accept_revisions_xml(xml: bytes) -> tuple[bytes, int]:
    """Принять исправления в одной части документа. Возвращает (XML, число исправлений)."""
    root = etree.fromstring(xml)
    count = 0
    for marker in list(root.iter(f"{{{W}}}del")):  # удалённая строка таблицы — целиком
        parent = marker.getparent()
        if parent is not None and parent.tag == f"{{{W}}}trPr":
            row = parent.getparent()
            if row is not None:
                _remove(row)
                count += 1
    for tag in (f"{{{W}}}del", f"{{{W}}}moveFrom"):  # удалённый и перенесённый «отсюда» текст
        for element in list(root.iter(tag)):
            parent = element.getparent()
            if parent is None:
                continue
            if parent.tag not in _PROPERTY_PARENTS:
                count += 1
            parent.remove(element)
    for tag in (f"{{{W}}}ins", f"{{{W}}}moveTo"):  # вставленный текст — остаётся без обёртки
        for element in list(root.iter(tag)):
            parent = element.getparent()
            if parent is None:
                continue
            if parent.tag in _PROPERTY_PARENTS:
                parent.remove(element)
                continue
            index = parent.index(element)
            for child in list(element):
                parent.insert(index, child)
                index += 1
            parent.remove(element)
            count += 1
    for element in [e for tag in _MARKERS for e in root.iter(tag)]:
        _remove(element)
    if not count:
        return xml, 0
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), count


def accept_docx_revisions(src: Path, dest: Path) -> int:
    """Копия DOCX с принятыми исправлениями. 0 — исправлений нет, `dest` не создаётся.

    Повреждённый архив — 0: дальше конвертация сама скажет понятную ошибку.
    """
    try:
        with zipfile.ZipFile(src) as archive:
            names = [n for n in archive.namelist() if _PARTS.match(n)]
            changed = {}
            for name in names:
                xml, count = accept_revisions_xml(archive.read(name))
                if count:
                    changed[name] = (xml, count)
            if not changed:
                return 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
                for info in archive.infolist():
                    data = changed[info.filename][0] if info.filename in changed else None
                    out.writestr(info, data if data is not None else archive.read(info.filename))
    except (zipfile.BadZipFile, etree.XMLSyntaxError, KeyError, OSError):
        return 0
    shutil.move(tmp, dest)
    return sum(count for _, count in changed.values())
