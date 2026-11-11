"""Подготовка одного источника в воркере, с сохранением оригинала для повтора."""

from __future__ import annotations

import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from svetofor.db.portfolio import PortfolioItem
from svetofor.pipeline.eis import fetch_package


def prepare_source(item: PortfolioItem, services: Any) -> tuple[Path, int]:
    settings = services.settings
    folder = Path(settings.packages_dir) / "_incoming"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(item.source_name).suffix if item.source_type == "file" else ".zip"
    incoming = folder / f"{uuid.uuid4()}{suffix}"
    try:
        if item.source_type == "eis":
            result = fetch_package(
                item.source_name,
                max_files=settings.max_files,
                max_bytes=settings.max_upload_bytes,
            )
            source_bytes = sum(len(data) for _, data in result.files)
            with zipfile.ZipFile(incoming, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in result.files:
                    archive.writestr(name, data)
        else:
            source = Path(item.source_path or "").resolve()
            root = (settings.data_dir / "portfolio_uploads").resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise FileNotFoundError("Исходный пакет недоступен")
            shutil.copyfile(source, incoming)
            source_bytes = incoming.stat().st_size
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise
    return incoming, source_bytes
