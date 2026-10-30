"""Любой входной файл → PDF.

Дальше у всех форматов один путь: текст по страницам, координаты для подсветки, один просмотрщик.
PDF проверяется (пароль, повреждение) и при необходимости расшифровывается; картинки — через
img2pdf без перекодирования; офисные форматы — LibreOffice в отдельном профиле на каждый запуск
(параллельные процессы LibreOffice с общим профилем мешают друг другу).
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

import img2pdf
import pymupdf
from lxml import etree
from PIL import Image, ImageSequence

from svetofor.core.tool_env import run_untrusted
from svetofor.pipeline.pdf import open_pdf

PDF_EXTENSIONS = frozenset({".pdf"})
OFFICE_EXTENSIONS = frozenset({".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".ods", ".txt"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"})
# XML из ЕИС («Электронный документ, полученный из внешней системы.xml», выгрузки извещений):
# читается как текст значений — без него пакет считался бы прочитанным не полностью
XML_EXTENSIONS = frozenset({".xml"})
SOFFICE_TIMEOUT = 120
SPREADSHEET_EXTENSIONS = frozenset({".xls", ".xlsx", ".ods"})
# лист целиком на одной странице: иначе LibreOffice режет широкую спецификацию по столбцам
# на разные страницы, и ряд «товар — количество — цена» распадается (LibreOffice ≥ 7.4)
_SINGLE_PAGE_SHEETS = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
_IMG2PDF_RETRY = (
    img2pdf.AlphaChannelError,
    img2pdf.ImageOpenError,
    img2pdf.UnsupportedColorspaceError,
    img2pdf.JpegColorspaceError,
)


class ConvertError(Exception):
    """Файл нельзя превратить в PDF. Сообщение — для пользователя."""


def convert_to_pdf(
    src: Path,
    extension: str,
    dest: Path,
    *,
    soffice: str = "soffice",
    timeout: int = SOFFICE_TIMEOUT,
    work_dir: Path | None = None,
) -> int:
    """Записать PDF в `dest`. Возвращает число страниц."""
    ext = extension.lower()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if ext in PDF_EXTENSIONS:
        _copy_pdf(src, dest)
    elif ext in IMAGE_EXTENSIONS:
        _image_to_pdf(src, dest)
    elif ext in OFFICE_EXTENSIONS:
        _office_to_pdf(src, ext, dest, soffice, timeout, work_dir)
    elif ext in XML_EXTENSIONS:
        _xml_to_pdf(src, dest, soffice, timeout, work_dir)
    else:
        raise ConvertError(f"Формат {ext or 'без расширения'} не поддерживается")
    return pdf_page_count(dest)


def pdf_page_count(path: Path) -> int:
    try:
        with open_pdf(path) as doc:
            if doc.needs_pass:
                raise ConvertError("Файл защищён паролем — снимите защиту и загрузите снова")
            count = int(doc.page_count)
    except ConvertError:
        raise
    except Exception as exc:  # pymupdf бросает разные исключения на битых файлах
        raise ConvertError("Файл повреждён или это не PDF") from exc
    if count == 0:
        raise ConvertError("В файле нет страниц")
    return count


def _copy_pdf(src: Path, dest: Path) -> None:
    pdf_page_count(src)  # пароль и повреждение — сразу понятной ошибкой
    with open_pdf(src) as doc:
        if doc.is_encrypted:
            # пароль только «на права» (печать, копирование): без расшифровки OCR откажется
            doc.save(dest, encryption=cast(Any, pymupdf).PDF_ENCRYPT_NONE)
            return
    shutil.copyfile(src, dest)


def _frames_as_png(src: Path) -> list[bytes]:
    """Все кадры картинки (многостраничный TIFF) в RGB PNG — для того, что img2pdf не берёт."""
    try:
        with Image.open(src) as image:
            frames = []
            for frame in ImageSequence.Iterator(image):
                buffer = io.BytesIO()
                frame.convert("RGB").save(buffer, format="PNG")
                frames.append(buffer.getvalue())
            return frames
    except Image.DecompressionBombError as exc:
        raise ConvertError("Изображение слишком большое") from exc
    except OSError as exc:
        raise ConvertError("Не удалось открыть изображение") from exc


def _image_to_pdf(src: Path, dest: Path) -> None:
    try:
        data = img2pdf.convert(str(src), rotation=img2pdf.Rotation.ifvalid)
    except _IMG2PDF_RETRY:
        data = img2pdf.convert(_frames_as_png(src), rotation=img2pdf.Rotation.ifvalid)
    except Image.DecompressionBombError as exc:
        raise ConvertError("Изображение слишком большое") from exc
    if data is None:
        raise ConvertError("Не удалось открыть изображение")
    dest.write_bytes(data)


def xml_text(raw: bytes) -> str:
    """Текстовые значения XML по строке. Чужой файл: без внешних сущностей, DTD и сети (XXE),
    сущности не раскрываются («миллиард смешков»)."""
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    try:
        root = etree.fromstring(_xml_source(raw), parser)
    except etree.XMLSyntaxError as exc:
        raise ConvertError("XML-файл повреждён") from exc
    if root is None:
        raise ConvertError("XML-файл пустой")
    lines = (" ".join(str(part).split()) for part in root.itertext())
    return "\n".join(line for line in lines if line)


def _xml_source(raw: bytes) -> bytes:
    """UTF-16 без метки порядка байт (так ЕИС отдаёт «документ из внешней системы») → UTF-8:
    libxml2 такую кодировку сам не распознаёт. Кодировку видно по нулевым байтам ASCII-символов."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:3] == b"\xef\xbb\xbf" or len(raw) < 4:
        return raw
    if raw[0] != 0 and raw[1] == 0 and raw[3] == 0:
        encoding = "utf-16-le"
    elif raw[0] == 0 and raw[2] == 0 and raw[1] != 0:
        encoding = "utf-16-be"
    else:
        return raw
    text = raw.decode(encoding, errors="replace")
    # объявление «encoding="UTF-16"» после перекодирования в UTF-8 было бы ложью
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8")


def _xml_to_pdf(src: Path, dest: Path, soffice: str, timeout: int, work_dir: Path | None) -> None:
    text = xml_text(src.read_bytes())
    if not text:
        raise ConvertError("В XML-файле нет текста")
    with tempfile.TemporaryDirectory(prefix="svetofor-xml-", dir=work_dir) as tmp:
        plain = Path(tmp) / "input.txt"
        plain.write_text(text, encoding="utf-8")
        _office_to_pdf(plain, ".txt", dest, soffice, timeout, work_dir)


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp866", errors="replace")


# «Скрытный режим» профиля LibreOffice: документ не подгружает внешние ресурсы (картинки и
# связи по адресам из документа) — защита от утечек и SSRF при открытии чужих файлов;
# макросы не выполняются: уровень «очень высокий» и вдобавок запрет выполнения целиком — даже
# из «доверенных» мест (DisableMacrosExecution есть в схеме LibreOffice 7.4 образа, 29.10.2026)
PROFILE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" \
xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
"""


def prepare_profile(profile: Path) -> None:
    """Профиль LibreOffice с безопасными настройками — до первого запуска в нём."""
    settings = profile / "user" / "registrymodifications.xcu"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(PROFILE_SETTINGS, encoding="utf-8")


def soffice_command(
    soffice: str, source: Path, out_dir: Path, profile: Path, *, plain: bool = False
) -> list[str]:
    command = [
        soffice,
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
    ]
    if source.suffix == ".txt":
        command.append("--infilter=Text (encoded):UTF8")
    target = "pdf"
    if source.suffix in SPREADSHEET_EXTENSIONS and not plain:
        target = _SINGLE_PAGE_SHEETS
    return [*command, "--convert-to", target, "--outdir", str(out_dir), str(source)]


def _office_to_pdf(
    src: Path, ext: str, dest: Path, soffice: str, timeout: int, work_dir: Path | None
) -> None:
    with tempfile.TemporaryDirectory(prefix="svetofor-lo-", dir=work_dir) as tmp:
        tmp_dir = Path(tmp)
        # латинское имя: LibreOffice выбирает фильтр по расширению, а кириллица в аргументах
        # командной строки — лишний риск
        source = tmp_dir / f"input{ext}"
        if ext == ".txt":
            source.write_text(_decode_text(src.read_bytes()), encoding="utf-8")
        else:
            shutil.copyfile(src, source)
        out_dir = tmp_dir / "out"
        for attempt in range(2):  # LibreOffice иногда падает на первом запуске профиля
            # вторая попытка — без особых параметров выгрузки (старый LibreOffice их не знает)
            profile = tmp_dir / "profile"
            prepare_profile(profile)
            command = soffice_command(soffice, source, out_dir, profile, plain=attempt > 0)
            try:
                run_untrusted(command, timeout=timeout)  # без секретов: документ — чужой
            except subprocess.TimeoutExpired:
                continue
            except FileNotFoundError as exc:
                raise ConvertError("Конвертация недоступна: не установлен LibreOffice") from exc
            result = out_dir / "input.pdf"
            if result.is_file() and result.stat().st_size > 0:
                shutil.move(result, dest)
                return
    raise ConvertError("Не удалось открыть файл")


_HEADING_STYLE = re.compile(r"^(?:heading|заголовок)\s*(\d)$", re.IGNORECASE)


def _outline_level(element: Any) -> int | None:
    """Уровень структуры из `w:outlineLvl` (0 — верхний; 9 — обычный текст)."""
    from docx.oxml.ns import qn

    ppr = element.find(qn("w:pPr")) if element is not None else None
    lvl = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
    if lvl is None:
        return None
    value = int(lvl.get(qn("w:val"), "9"))
    return value + 1 if value < 9 else None


def _heading_level(paragraph: Any) -> int | None:
    level = _outline_level(paragraph._p)
    if level is not None:
        return level
    style = paragraph.style
    while style is not None:
        match = _HEADING_STYLE.match(style.name or "")
        if match:
            return int(match.group(1))
        level = _outline_level(style.element)
        if level is not None:
            return level
        style = style.base_style
    return None


def docx_heading_hints(path: Path) -> dict[str, int]:
    """Заголовки из стилей DOCX (самый надёжный признак структуры): текст → уровень."""
    import docx

    try:
        document = docx.Document(str(path))
    except Exception:  # битый или не DOCX — просто без подсказок
        return {}
    hints: dict[str, int] = {}
    for paragraph in document.paragraphs:
        text = " ".join(paragraph.text.split())
        if not text or len(text) > 200:
            continue
        level = _heading_level(paragraph)
        if level is not None:
            hints.setdefault(text, level)
    return hints


def office_to_pdf(
    src: Path,
    dest: Path,
    *,
    soffice: str = "soffice",
    timeout: int = SOFFICE_TIMEOUT,
    work_dir: Path | None = None,
) -> None:
    """Офисный документ → PDF через LibreOffice (для выгрузки отчёта в PDF)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _office_to_pdf(src, src.suffix.lower(), dest, soffice, timeout, work_dir)
