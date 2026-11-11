"""Потоковый приём отдельных пакетов: границы портфеля проверяются на сервере."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from svetofor.api.errors import AppError
from svetofor.core.config import get_settings
from svetofor.pipeline.eis import EisError, normalize_number
from svetofor.pipeline.unpack import ARCHIVE_EXTENSIONS, SUPPORTED_EXTENSIONS, repair_filename


@dataclass
class Source:
    source_type: str
    source_name: str
    source_path: str | None = None
    source_sha256: str | None = None
    source_bytes: int = 0


def number_sources(numbers: str) -> list[Source]:
    if not get_settings().eis_fetch:
        raise AppError("eis_disabled", "Загрузка из ЕИС отключена", 409)
    result = []
    seen = set()
    for line in numbers.splitlines():
        if not line.strip():
            continue
        try:
            number = normalize_number(line.strip())
        except EisError as exc:
            raise AppError("bad_number", str(exc), 400) from exc
        if number not in seen:
            result.append(Source("eis", number))
            seen.add(number)
    return result


def validate_count(count: int) -> None:
    if not 1 <= count <= get_settings().max_portfolio_items:
        raise AppError(
            "portfolio_count",
            f"Выберите от 1 до {get_settings().max_portfolio_items} закупок",
            400,
        )


async def file_sources(files: list[UploadFile], folder: Path) -> list[Source]:
    validate_count(len(files))
    settings = get_settings()
    result = []
    total = 0
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for upload in files:
            name = repair_filename(upload.filename or "")
            name = Path(name.replace("\\", "/")).name[:500]
            suffix = Path(name).suffix.lower()
            if suffix not in ARCHIVE_EXTENSIONS | SUPPORTED_EXTENSIONS:
                raise AppError("unsupported_format", "Неподдерживаемый формат пакета", 415)
            target = folder / f"{uuid.uuid4()}{suffix}"
            size = 0
            digest = hashlib.sha256()
            with target.open("wb") as stream:
                while block := await upload.read(1024 * 1024):
                    size += len(block)
                    total += len(block)
                    if (
                        size > settings.max_upload_bytes
                        or total > settings.max_portfolio_mb * 1024**2
                    ):
                        raise AppError("too_large", "Превышен размер пакета или портфеля", 413)
                    digest.update(block)
                    stream.write(block)
            if not size:
                raise AppError("empty_file", "Один из файлов пустой", 400)
            result.append(Source("file", name, str(target), digest.hexdigest(), size))
    except BaseException:
        # Каталог создан этим запросом, его UUID не берётся из имени загруженного файла.
        shutil.rmtree(folder)
        raise
    return result


def request_hash(title: str, sources: list[Source]) -> str:
    payload = [title, [(s.source_type, s.source_name, s.source_sha256) for s in sources]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
