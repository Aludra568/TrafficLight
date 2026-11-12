"""Сборка независимой редакции из серверных оригиналов, не меняя историю."""

import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.core.config import Settings
from svetofor.db.models import Check, Document
from svetofor.db.portfolio import PortfolioOperation
from svetofor.pipeline.storage import PackagePaths, safe_filename


def archive_name(index: int, filename: str) -> str:
    base = PurePosixPath(filename.replace("\\", "/")).name
    suffix = PurePosixPath(base).suffix.lower()
    stem = safe_filename(PurePosixPath(base).stem)[:120]
    return f"{index + 1:04}_{stem}{suffix}"


def assemble(
    factory: Callable[[], Session], operation: PortfolioOperation, settings: Settings
) -> Path:
    payload = operation.payload
    entries: list[tuple[str, Path]] = []
    if payload["check_id"]:
        with factory() as db:
            check = db.get(Check, uuid.UUID(payload["check_id"]))
            if check is None:
                raise ValueError("Предыдущая проверка удалена")
            package = check.package
            root = Path(package.storage_path).resolve()
            if not root.is_relative_to(settings.packages_dir.resolve()):
                raise ValueError("Некорректный путь пакета")
            docs = db.scalars(
                select(Document)
                .where(Document.package_id == package.id)
                .order_by(Document.relative_path, Document.id)
            )
            for doc in docs:
                if str(doc.id) in payload["remove_ids"]:
                    continue
                source = PackagePaths(root).source_for(doc.id, doc.filename).resolve()
                if not source.is_relative_to(root) or not source.is_file():
                    raise ValueError("Исходный документ недоступен")
                entries.append((doc.filename, source))
    uploads_root = (settings.data_dir / "portfolio_uploads").resolve()
    for source in payload["sources"]:
        path = Path(source["source_path"]).resolve()
        if not path.is_relative_to(uploads_root) or not path.is_file():
            raise ValueError("Загруженный файл недоступен")
        entries.append((source["source_name"], path))
    if not entries or len(entries) > settings.max_files:
        raise ValueError("Недопустимое число документов")
    if sum(path.stat().st_size for _, path in entries) > settings.max_upload_bytes:
        raise ValueError("Превышен размер пакета")
    folder = attempt_folder(operation, settings)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "bundle.zip"
    temporary = folder / f"{uuid.uuid4()}.tmp"
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, (name, path) in enumerate(entries):
                archive.write(path, archive_name(index, name))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def cleanup_uploads(operation: PortfolioOperation, settings: Settings) -> None:
    root = (settings.data_dir / "portfolio_uploads").resolve()
    for source in operation.payload["sources"]:
        path = Path(source["source_path"]).resolve()
        if path.is_relative_to(root):
            path.unlink(missing_ok=True)


def attempt_folder(operation: PortfolioOperation, settings: Settings) -> Path:
    if operation.started_at is None:
        raise ValueError("Подготовка не запущена")
    stamp = operation.started_at.strftime("%Y%m%d%H%M%S%f")
    return settings.data_dir / "portfolio_uploads" / str(operation.id) / stamp


def cleanup_attempt(operation: PortfolioOperation, settings: Settings) -> None:
    cleanup_uploads(operation, settings)
    folder = attempt_folder(operation, settings)
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file() and (path.name == "bundle.zip" or path.suffix == ".tmp"):
                path.unlink(missing_ok=True)
        folder.rmdir()
