"""Проверка критериев приёмки задачи 1 на запущенном приложении → таблица с цифрами.

Только стандартная библиотека Python 3.10+ (как `smoke_demo.py`):
    python scripts/acceptance.py                       # стек Docker, http://localhost:8080
    python scripts/acceptance.py http://127.0.0.1:8000  # режим разработки
    python scripts/acceptance.py --package20 пакет.zip --out eval/results/acceptance.json

Что измеряется здесь: 1 — PDF на 60 страниц (время ответа на загрузку, обработка без сбоя),
2 — правило без кода (черновик в песочнице, без сохранения в набор), 5 — пакет на 20 страниц
с моделью (`--package20`, нужен ключ), 6 — у замечаний с цитатой есть раздел и страница.
Критерии 3, 4 и 7 проверяются иначе: интерфейс — глазами, OCR — `make eval-ocr`,
одна команда запуска — CI (`docker compose up --build`). Код возврата 1, если что-то не прошло.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_demo import (  # скрипты лежат рядом, пакета нет
    PDF_PACKAGE,
    SmokeError,
    get_json,
    login,
    request,
    upload,
    wait_finished,
)

LONG_PAGES = 60
UPLOAD_LIMIT_S = 5.0
PROCESS_LIMIT_S = 120.0  # критерий 5: 20 страниц < 2 минут


@dataclass
class Row:
    number: int
    criterion: str
    result: str
    passed: bool | None  # None — здесь не измеряется


def make_pdf(pages: int) -> bytes:
    """PDF с текстом на каждой странице — без сторонних библиотек (шрифт Helvetica, латиница)."""
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(pages):
        lines = [
            f"Section {i + 1}. Terms of supply, acceptance and payment, item {j}."
            for j in range(40)
        ]
        text = " ".join(f"({line}) Tj T*" for line in lines)
        stream = f"BT /F1 10 Tf 14 TL 50 790 Td {text} ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def timed_check(
    api: str, token: str, package: Path, timeout_s: int
) -> tuple[float, float, dict[str, Any]]:
    started = time.monotonic()
    check = upload(api, token, package)
    uploaded = time.monotonic() - started
    state = wait_finished(api, token, check, timeout_s)
    return uploaded, time.monotonic() - started, {"check_id": check, **state}


def criterion_1(api: str, token: str, work: Path, timeout_s: int) -> Row:
    pdf = work / f"acceptance_{LONG_PAGES}_pages.pdf"
    pdf.write_bytes(make_pdf(LONG_PAGES))
    try:
        uploaded, total, state = timed_check(api, token, pdf, timeout_s)
    except SmokeError as exc:
        return Row(1, f"Файл {LONG_PAGES} страниц без 504", f"сбой: {exc}", False)
    measured = (
        f"ответ на загрузку {uploaded:.1f} с, обработка {total:.0f} с, итог {state['status']}"
    )
    if total - uploaded < 1.0 and uploaded >= UPLOAD_LIMIT_S:
        # проверка закончилась внутри запроса загрузки: режим разработки (задачи в процессе API),
        # очереди нет — время ответа здесь не про 504; критерий меряется на стеке Docker
        return Row(1, f"Файл {LONG_PAGES} страниц без 504", measured + " — режим разработки", None)
    return Row(1, f"Файл {LONG_PAGES} страниц без 504", measured, uploaded < UPLOAD_LIMIT_S)


SANDBOX_RULE = {
    "id": "TST-01",
    "title": "Приёмка только в рабочие часы заказчика",
    "category": "acceptance",
    "law": ["44-ФЗ", "223-ФЗ"],
    "applies_to": ["contract"],
    "type": "pattern",
    "patterns": ["(?i)при[её]мк\\w*[^.]{0,60}?рабоч\\w+\\s+(?:час|врем)"],
    "scope": "chunk",
    "severity": "yellow",
    "message": "Приёмка ограничена рабочим временем заказчика: {quote}",
    "why_risk": "Поставку вне рабочих часов не примут — простой транспорта за счёт поставщика.",
    "recommendation": "Согласовать окна поставки заранее.",
    "status": "draft",
}
SANDBOX_TEXT = (
    "5.2. Приёмка товара осуществляется только в рабочие часы заказчика с 9:00 до 17:00. "
    "5.3. Оплата производится в течение 7 рабочих дней."
)


def criterion_2(api: str, token: str) -> Row:
    title = "Новое правило без кода"
    body = json.dumps({"rule": SANDBOX_RULE, "text": SANDBOX_TEXT}).encode()
    task = json.loads(request(f"{api}/rules/test", token, body, "application/json"))["task_id"]
    for _ in range(60):
        answer = get_json(f"{api}/rules/test/{task}", token)
        if answer["status"] != "pending":
            break
        time.sleep(1)
    result = answer.get("result") or {}
    found = bool(result.get("findings"))
    detail = f"черновик правила в песочнице: исход {result.get('outcome')}, замечаний {len(result.get('findings') or [])}"
    return Row(2, title, detail + " (без сохранения в набор)", answer["status"] == "done" and found)


def criterion_5(api: str, token: str, package: Path | None, timeout_s: int) -> Row:
    title = "20 страниц < 2 минут"
    try:
        ready = json.loads(urllib.request.urlopen(f"{api}/ready", timeout=30).read())
    except urllib.error.HTTPError as exc:  # 503 «degraded» (нет Redis/эмбеддингов) — тело то же
        ready = json.loads(exc.read())
    if not ready.get("llm", {}).get("configured"):
        return Row(5, title, "не измерено: модель не подключена (нужен ключ в .env)", None)
    if package is None:
        return Row(5, title, "не измерено: укажите пакет на 20 страниц (--package20)", None)
    _, total, state = timed_check(api, token, package, timeout_s)
    passed = total < PROCESS_LIMIT_S and state["status"] in {"done", "done_with_warnings"}
    return Row(5, title, f"{package.name}: {total:.0f} с, итог {state['status']}", passed)


def criterion_6(api: str, token: str, timeout_s: int) -> Row:
    _, _, state = timed_check(api, token, PDF_PACKAGE, timeout_s)
    findings = get_json(f"{api}/checks/{state['check_id']}/report", token)["findings"]
    quoted = [f for f in findings if f.get("quote")]
    located = [f for f in quoted if f.get("page_from") is not None and f.get("section_path")]
    return Row(
        6,
        "Ссылка на раздел",
        f"демо-пакет: у {len(located)} из {len(quoted)} замечаний с цитатой есть раздел и страница",
        bool(quoted) and len(located) == len(quoted),
    )


MEASURED_ELSEWHERE = [
    Row(3, "UX для не-технических", "глазами: 1440 и 390 px", None),
    Row(4, "OCR ≥ 95 %", "`make eval-ocr` (Tesseract в Docker-образе)", None),
    Row(7, "`docker compose up --build`", "задание docker в CI", None),
]


def table(rows: list[Row]) -> str:
    mark = {True: "✅", False: "❌", None: "—"}
    lines = ["| № | Критерий | Результат | Итог |", "|---|---|---|---|"]
    for r in sorted(rows + MEASURED_ELSEWHERE, key=lambda r: r.number):
        lines.append(f"| {r.number} | {r.criterion} | {r.result} | {mark[r.passed]} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?", default="http://localhost:8080")
    parser.add_argument("--package20", type=Path, help="пакет на ~20 страниц для критерия 5")
    parser.add_argument("--out", type=Path, help="сохранить результат в JSON")
    parser.add_argument("--timeout", type=int, default=600, help="секунд на одну проверку")
    args = parser.parse_args()
    api = args.base.rstrip("/") + "/api"
    work = Path(__file__).resolve().parent.parent / ".cache"
    work.mkdir(exist_ok=True)
    try:
        token = login(api)
        rows = [
            criterion_1(api, token, work, args.timeout),
            # песочница правил — инструмент методолога (специалисту — 403)
            criterion_2(api, login(api, "methodologist@demo.ru")),
            criterion_5(api, token, args.package20, args.timeout),
            criterion_6(api, token, args.timeout),
        ]
    except (SmokeError, OSError, KeyError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    print(table(rows))
    if args.out:
        args.out.write_text(
            json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 1 if any(r.passed is False for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
