"""Клиент ЕИС: сеть подменена — ни одного запроса наружу."""

from __future__ import annotations

import hashlib
import ssl
import urllib.parse
from collections.abc import Callable

import httpx
import pytest

from svetofor.pipeline import eis
from svetofor.pipeline.eis import EisError, fetch_package, make_client, normalize_number

N44 = "0362100026126000023"
N223 = "32616529449"
GUID = "1acd7341-0c0d-498b-81ef-5d2bcf70460a"
UID1, UID2 = "01A15CF950AA718799DA5D9C690DAB4F", "01A15CF6910F77BFBF0053A064757572"
MB = 1024 * 1024


def link(uid: str, law: str = "44fz", kind: str = "priz", host: str = "zakupki.gov.ru") -> str:
    return (
        f'<a href="https://{host}/{law}/filestore/public/1.0/download/{kind}/file.html?uid={uid}">'
    )


def attachment(name: str, body: bytes) -> httpx.Response:
    quoted = urllib.parse.quote(name)
    return httpx.Response(
        200,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
        content=body,
    )


Route = Callable[[httpx.Request], httpx.Response]


def fake_eis(routes: dict[str, Route | httpx.Response], seen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.host == "zakupki.gov.ru", f"запрос не в ЕИС: {request.url}"
        key = request.url.raw_path.decode()
        route = routes.get(key)
        if route is None:
            return httpx.Response(404, text="нет")
        return route(request) if callable(route) else route

    return make_client(httpx.MockTransport(handler))


def eis44(seen: list[str], **extra: Route | httpx.Response) -> httpx.Client:
    routes: dict[str, Route | httpx.Response] = {
        # ЕИС перенаправляет общий адрес на страницу нужного способа закупки
        f"/epz/order/notice/view/documents.html?regNumber={N44}": httpx.Response(
            302, headers={"location": f"/epz/order/notice/ea20/view/documents.html?regNumber={N44}"}
        ),
        f"/epz/order/notice/ea20/view/documents.html?regNumber={N44}": httpx.Response(
            200,
            text=link(UID1) + link(UID2) + link(UID1),  # повтор ссылки — один файл
        ),
        f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID1}": attachment(
            "Проект контракта.docx", b"PK contract"
        ),
        f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID2}": attachment(
            "Описание объекта закупки.docx", b"PK spec"
        ),
        f"/epz/order/notice/printForm/view.html?regNumber={N44}": httpx.Response(
            200, text="<html><script>x()</script><p>НМЦК: 1 000 000,00</p><td>Аванс</td></html>"
        ),
    }
    routes.update(extra)
    return fake_eis(routes, seen)


def fetch(client: httpx.Client, number: str = N44, **kw: int) -> eis.EisPackage:
    return fetch_package(
        number, max_files=kw.get("max_files", 10), max_bytes=kw.get("max_bytes", MB), client=client
    )


def test_number_formats() -> None:
    assert normalize_number(" № 0362 1000 2612 6000 023 ") == N44
    assert normalize_number(N223) == N223
    for bad in ("", "123", "0362100026126000023x", "1" * 20, "../etc", "03621000261260000２3"):
        with pytest.raises(EisError, match="19 цифр"):
            normalize_number(bad)


def test_44fz_package_with_notice_text() -> None:
    seen: list[str] = []
    package = fetch(eis44(seen))
    assert package.law == "44-ФЗ"
    names = [name for name, _ in package.files]
    assert names == [eis.NOTICE_FILE, "Проект контракта.docx", "Описание объекта закупки.docx"]
    notice = package.files[0][1].decode()
    assert "НМЦК: 1 000 000,00" in notice and "x()" not in notice  # скрипты печатной формы — нет
    assert sum("uid=" + UID1 in url for url in seen) == 1


def test_223fz_package_through_notice_guid() -> None:
    seen: list[str] = []
    uid = "2CD4A4B7221141319473BB272B9A335E"
    client = fake_eis(
        {
            f"/epz/order/notice/notice223/common-info.html?regNumber={N223}": httpx.Response(
                200, text=f'<a href="/x?noticeGuid={GUID}">'
            ),
            f"/epz/order/notice/notice223/documents.html?purchaseNoticeNumber={N223}"
            f"&noticeGuid={GUID}": httpx.Response(200, text=link(uid, "223", "fz223")),
            f"/223/filestore/public/1.0/download/fz223/file.html?uid={uid}": attachment(
                "Извещение 21.docx", b"PK notice"
            ),
        },
        seen,
    )
    package = fetch(client, N223)
    assert package.law == "223-ФЗ"
    # печатная форма не открылась — пакет всё равно собран, без текста извещения
    assert [name for name, _ in package.files] == ["Извещение 21.docx"]


def test_links_to_other_hosts_are_rebuilt_for_eis() -> None:
    """Из страницы берётся только uid: ссылка на чужой сайт не уводит запрос туда (SSRF)."""
    seen: list[str] = []
    page = link(UID1, host="169.254.169.254") + link(UID2, host="evil.example")
    client = eis44(
        seen,
        **{
            f"/epz/order/notice/ea20/view/documents.html?regNumber={N44}": httpx.Response(
                200, text=page
            )
        },
    )
    fetch(client)
    assert all(urllib.parse.urlsplit(url).hostname == "zakupki.gov.ru" for url in seen)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/file",
        "http://zakupki.gov.ru/file",
        "//evil.example/x",
        "https://zakupki.gov.ru.evil.example/x",
    ],
)
def test_redirect_outside_eis_is_refused(location: str) -> None:
    seen: list[str] = []
    client = eis44(
        seen,
        **{
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID1}": httpx.Response(
                302, headers={"location": location}
            )
        },
    )
    with pytest.raises(EisError, match="другой сайт"):
        fetch(client)
    assert all(urllib.parse.urlsplit(url).hostname == "zakupki.gov.ru" for url in seen)


def test_size_limit_counts_real_bytes_not_header() -> None:
    seen: list[str] = []
    big = httpx.Response(
        200,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''big.pdf",
            "Content-Length": "10",
        },
        content=b"0" * (MB + 1),
    )
    client = eis44(seen, **{f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID1}": big})
    with pytest.raises(EisError, match="больше 1 МБ"):
        fetch(client)


def test_total_size_limit_over_all_files() -> None:
    seen: list[str] = []
    half = MB // 2 + 1
    client = eis44(
        seen,
        **{
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID1}": attachment(
                "a.pdf", b"0" * half
            ),
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID2}": attachment(
                "b.pdf", b"0" * half
            ),
        },
    )
    with pytest.raises(EisError, match="больше 1 МБ"):
        fetch(client)


def test_same_file_in_several_editions_is_kept_once() -> None:
    """223-ФЗ: файл каждой редакции извещения — под своим uid, но с тем же содержимым."""
    seen: list[str] = []
    client = eis44(
        seen,
        **{
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID2}": attachment(
                "Проект контракта.docx", b"PK contract"
            )
        },
    )
    assert [name for name, _ in fetch(client).files] == [eis.NOTICE_FILE, "Проект контракта.docx"]


def test_too_many_files() -> None:
    with pytest.raises(EisError, match="больше 1 файлов"):
        fetch(eis44([]), max_files=1)


def test_dangerous_and_repeated_names() -> None:
    seen: list[str] = []
    client = eis44(
        seen,
        **{
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID1}": attachment(
                "../../etc/passwd\r\n.docx", b"PK 1"
            ),
            f"/44fz/filestore/public/1.0/download/priz/file.html?uid={UID2}": attachment(
                "..\\..\\etc/passwd\r\n.docx", b"PK 2"
            ),
        },
    )
    names = [name for name, _ in fetch(client).files[1:]]
    assert all("/" not in n and "\\" not in n and "\n" not in n and ".." not in n for n in names)
    assert len(set(names)) == 2


def test_eis_unavailable_and_missing() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи", request=request)

    with pytest.raises(EisError, match="недоступна"):
        fetch(make_client(httpx.MockTransport(down)))
    with pytest.raises(EisError, match="не найдена"):
        fetch(fake_eis({}, []))
    empty = {f"/epz/order/notice/view/documents.html?regNumber={N44}": httpx.Response(200, text="")}
    with pytest.raises(EisError, match="нет документов"):
        fetch(fake_eis(empty, []))  # type: ignore[arg-type]


def test_pinned_root_is_the_ministry_root() -> None:
    """В клиенте ЕИС — проверка сертификата по корню Минцифры, отпечаток сверен с crt.sh."""
    pem = eis.CA_FILE.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN") :])
    assert hashlib.sha256(der).hexdigest().upper() == (
        "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"
    )
    context = ssl.create_default_context(cafile=str(eis.CA_FILE))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert len(context.get_ca_certs()) == 1  # доверие только этому корню, не всему хранилищу
