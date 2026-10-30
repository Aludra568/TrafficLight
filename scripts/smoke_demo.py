"""Сквозная проверка запущенного приложения: вход, загрузка демо-пакетов, итог проверки.

Только стандартная библиотека Python 3.10+, работает в CI и на Windows:
    python scripts/smoke_demo.py                         # стек Docker, http://localhost:8080
    python scripts/smoke_demo.py http://127.0.0.1:8000 --pdf-only   # режим разработки

Без ключа модели смысловые правила — «не проверено», поэтому проверяется детерминированная
часть: красный итог, замечание CON-01 (срок поставки в ТЗ и в контракте), у цитат есть страница,
отчёт выгружается в Word. Word-пакет требует LibreOffice (есть в Docker-образе).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PDF_PACKAGE = ROOT / "samples" / "demo_44fz_postavka_pdf.zip"
DOCX_PACKAGE = ROOT / "samples" / "demo_44fz_postavka.zip"
FINISHED = {"done", "done_with_warnings"}


class SmokeError(Exception):
    pass


def request(
    url: str, token: str | None = None, data: bytes | None = None, content_type: str | None = None
) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SmokeError(f"{req.method} {url}: HTTP {exc.code} {body}") from exc


def get_json(url: str, token: str | None = None) -> Any:
    return json.loads(request(url, token))


def login(api: str, email: str = "specialist@demo.ru") -> str:
    body = json.dumps({"email": email, "password": "demo"}).encode()
    token = json.loads(request(f"{api}/auth/login", data=body, content_type="application/json"))
    if not token.get("access_token"):
        raise SmokeError("вход не вернул токен")
    return str(token["access_token"])


def upload(api: str, token: str, package: Path) -> str:
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{package.name}"\r\nContent-Type: application/zip\r\n\r\n'
    ).encode()
    body = head + package.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    answer = json.loads(
        request(f"{api}/checks", token, body, f"multipart/form-data; boundary={boundary}")
    )
    if not answer.get("check_id"):
        raise SmokeError("загрузка не вернула check_id")
    return str(answer["check_id"])


def wait_finished(api: str, token: str, check: str, timeout_s: int) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        state = get_json(f"{api}/checks/{check}", token)
        if state["status"] in FINISHED:
            return dict(state)
        if state["status"] == "failed":
            raise SmokeError("проверка завершилась с ошибкой")
        if time.monotonic() - started > timeout_s:
            raise SmokeError(f"проверка не закончилась за {timeout_s} с (статус {state['status']})")
        time.sleep(2)


def check_package(api: str, token: str, package: Path, timeout_s: int) -> None:
    print(f"— пакет {package.name}")
    started = time.monotonic()
    check = upload(api, token, package)
    print(f"  ответ на загрузку: {time.monotonic() - started:.1f} с")
    state = wait_finished(api, token, check, timeout_s)
    print(
        f"  итог: {state['status']}, светофор {state['light']} "
        f"за {time.monotonic() - started:.1f} с"
    )
    if state["light"] != "red":
        raise SmokeError(f"ожидался красный итог, получен {state['light']}")

    findings = get_json(f"{api}/checks/{check}/report", token)["findings"]
    if not any(f.get("rule_code") == "CON-01" for f in findings):
        raise SmokeError("нет замечания CON-01 (срок поставки в ТЗ и в контракте)")
    if any(f.get("quote") and f.get("page_from") is None for f in findings):
        raise SmokeError("у замечания с цитатой нет страницы")
    print(f"  замечаний: {len(findings)}")

    if not request(f"{api}/checks/{check}/export?format=docx", token, data=b""):
        raise SmokeError("отчёт в Word пустой")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?", default="http://localhost:8080")
    parser.add_argument("--pdf-only", action="store_true", help="без Word-пакета (нет LibreOffice)")
    parser.add_argument("--timeout", type=int, default=300, help="секунд на одну проверку")
    args = parser.parse_args()
    api = args.base.rstrip("/") + "/api"
    packages = [PDF_PACKAGE] if args.pdf_only else [PDF_PACKAGE, DOCX_PACKAGE]
    try:
        get_json(f"{api}/health")
        token = login(api)
        for package in packages:
            check_package(api, token, package, args.timeout)
    except (SmokeError, urllib.error.URLError, KeyError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    print("Демо-сценарий пройден.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
