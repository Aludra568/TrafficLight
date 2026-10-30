"""Закупка из ЕИС по реестровому номеру: текст извещения и вложения.

Клиент ходит только на https://zakupki.gov.ru и сам собирает адреса: из страниц ЕИС берутся лишь
uid файлов и guid извещения — защита от SSRF. Сертификат ЕИС выдан УЦ Минцифры; доверие этому
корню — только здесь (`certs/russian_trusted_root_ca.pem`), проверка сертификата не отключается.
"""

from __future__ import annotations

import hashlib
import html
import re
import ssl
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from svetofor.core.logging import get_logger
from svetofor.pipeline.storage import safe_filename

log = get_logger(__name__)

HOST = "zakupki.gov.ru"
BASE = f"https://{HOST}"
CA_FILE = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"
NOTICE_FILE = "Извещение (печатная форма ЕИС).txt"
_UID = re.compile(
    r"/(44fz|223)/filestore/public/1\.0/download/(priz|fz223)/file\.html\?uid=([0-9A-F]{32})"
)
_GUID = re.compile(r"noticeGuid=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_MAX_REDIRECTS = 3
_USER_AGENT = "Mozilla/5.0 (compatible; Svetofor/1.0; +procurement risk check)"
UNAVAILABLE = (
    "ЕИС сейчас недоступна (сайт мог закрыть доступ с этого адреса). "
    "Скачайте документы закупки вручную и загрузите их архивом"
)


class EisError(Exception):
    """Понятная пользователю причина, по которой закупку не удалось загрузить."""


class _TooBigError(Exception):
    """Ответ ЕИС больше оставшегося лимита — сообщение с общим лимитом пишет `fetch_package`."""


@dataclass
class EisPackage:
    law: str  # «44-ФЗ» или «223-ФЗ»
    number: str
    files: list[tuple[str, bytes]] = field(default_factory=list)


def normalize_number(raw: str) -> str:
    """Реестровый номер: 19 цифр — 44-ФЗ, 11 цифр — 223-ФЗ; пробелы и «№» допускаются."""
    number = re.sub(r"[\s№#]", "", raw or "")
    # [0-9], а не \d: \d пропускает цифры других письменностей («２», «٣»)
    if not re.fullmatch(r"[0-9]{19}|[0-9]{11}", number):
        raise EisError("Номер закупки — 19 цифр (44-ФЗ) или 11 цифр (223-ФЗ)")
    return number


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    context = ssl.create_default_context(cafile=str(CA_FILE))
    return httpx.Client(
        verify=context,
        transport=transport,
        follow_redirects=False,  # перенаправления — вручную и только внутри ЕИС
        timeout=httpx.Timeout(60.0, connect=20.0),
        headers={"User-Agent": _USER_AGENT},
        trust_env=False,  # без прокси и сертификатов из окружения
    )


def fetch_package(
    number: str, *, max_files: int, max_bytes: int, client: httpx.Client | None = None
) -> EisPackage:
    """Скачать извещение и вложения. Ошибки сети и ЕИС — `EisError` с причиной по-русски."""
    own = client is None
    http = client or make_client()
    try:
        return _fetch(http, normalize_number(number), max_files, max_bytes)
    except _TooBigError as exc:
        megabytes = max_bytes // (1024 * 1024)
        raise EisError(
            f"Документы закупки больше {megabytes} МБ — загрузите нужные архивом"
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("eis_unavailable", number=number, error=type(exc).__name__)
        raise EisError(UNAVAILABLE) from exc
    finally:
        if own:
            http.close()


def _fetch(http: httpx.Client, number: str, max_files: int, max_bytes: int) -> EisPackage:
    if len(number) == 19:
        package = EisPackage("44-ФЗ", number)
        page = _get_text(
            http, f"/epz/order/notice/view/documents.html?regNumber={number}", max_bytes
        )
        notice = f"/epz/order/notice/printForm/view.html?regNumber={number}"
    else:
        package = EisPackage("223-ФЗ", number)
        info = _get_text(
            http, f"/epz/order/notice/notice223/common-info.html?regNumber={number}", max_bytes
        )
        guid = _GUID.search(info)
        if guid is None:
            raise EisError(f"Закупка № {number} в ЕИС не найдена")
        query = f"purchaseNoticeNumber={number}&noticeGuid={guid.group(1)}"
        page = _get_text(http, f"/epz/order/notice/notice223/documents.html?{query}", max_bytes)
        notice = (
            "/epz/order/notice/notice223/printForm/view.html"
            f"?purchaseNoticeGuid={guid.group(1)}&purchaseNoticeNumber={number}"
        )
    links = list(dict.fromkeys(m.group(0) for m in _UID.finditer(page)))
    if not links:
        raise EisError(f"У закупки № {number} в ЕИС нет документов для скачивания")
    if len(links) > max_files:
        raise EisError(f"В закупке больше {max_files} файлов — загрузите нужные архивом")
    total = 0
    names: set[str] = set()
    digests: set[str] = set()
    for path in links:
        name, data = _download(http, path, max_bytes - total)
        total += len(data)
        # ЕИС показывает файл в каждой редакции извещения под разными uid — копия не нужна
        digest = hashlib.sha256(data).hexdigest()
        if digest in digests:
            continue
        digests.add(digest)
        package.files.append((_unique(name, names), data))
    text = _notice_text(http, notice, max_bytes)
    if text:
        package.files.insert(0, (NOTICE_FILE, text.encode("utf-8")))
    log.info("eis_fetched", number=number, files=len(package.files), bytes=total)
    return package


def _request(http: httpx.Client, path: str) -> Iterator[httpx.Response]:
    """GET по пути на zakupki.gov.ru; перенаправление — только на тот же сайт по HTTPS."""
    url = BASE + path
    for _ in range(_MAX_REDIRECTS + 1):
        with http.stream("GET", url) as response:
            if response.is_redirect:
                target = urllib.parse.urljoin(url, response.headers.get("location", ""))
                parts = urllib.parse.urlsplit(target)
                if parts.scheme != "https" or parts.hostname != HOST:
                    raise EisError("ЕИС перенаправила на другой сайт — загрузка остановлена")
                url = target
                continue
            if response.status_code == 404:
                raise EisError("Закупка в ЕИС не найдена — проверьте номер")
            response.raise_for_status()
            yield response
            return
    raise EisError("ЕИС перенаправляет по кругу — загрузка остановлена")


def _read(response: httpx.Response, limit: int) -> bytes:
    """Тело потоком с обрывом на лимите: Content-Length может врать."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise _TooBigError
        chunks.append(chunk)
    return b"".join(chunks)


def _get_text(http: httpx.Client, path: str, limit: int) -> str:
    for response in _request(http, path):
        return _read(response, limit).decode("utf-8", "replace")
    raise EisError(UNAVAILABLE)  # pragma: no cover — _request либо отдаёт ответ, либо бросает


def _download(http: httpx.Client, path: str, limit: int) -> tuple[str, bytes]:
    for response in _request(http, path):
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I) or re.search(
            r'filename="([^"]+)"', disposition, re.I
        )
        raw = urllib.parse.unquote(match.group(1)) if match else path.rsplit("=", 1)[-1]
        return safe_filename(raw, default="документ"), _read(response, limit)
    raise EisError(UNAVAILABLE)  # pragma: no cover


def _unique(name: str, taken: set[str]) -> str:
    stem, dot, suffix = name.rpartition(".")
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem} ({n}).{suffix}" if dot else f"{name} ({n})"
    taken.add(candidate.lower())
    return candidate


def _notice_text(http: httpx.Client, path: str, limit: int) -> str:
    """Текст печатной формы извещения; без неё пакет всё равно проверяется."""
    try:
        page = _get_text(http, path, limit)
    except (EisError, _TooBigError, httpx.HTTPError) as exc:
        log.info("eis_notice_unavailable", error=type(exc).__name__)
        return ""
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", page)
    page = re.sub(r"(?i)</t[dh]>", " \t", page)
    text = html.unescape(re.sub(r"<[^>]+>", "", page))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
