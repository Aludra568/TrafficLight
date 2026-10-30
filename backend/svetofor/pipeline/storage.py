"""Где лежат файлы пакета.

/data/packages/{package_id}/
  original/          загруженный файл как есть
  unpacked/          файлы из архива, по одному на документ: {document_id}{расширение}
  pdf/               нормализованный PDF с текстовым слоем: {document_id}.pdf
  reports/           выгруженные отчёты
  tmp/               рабочие файлы конвертации и OCR
"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class PackagePaths:
    root: Path

    @property
    def original(self) -> Path:
        return self.root / "original"

    @property
    def unpacked(self) -> Path:
        return self.root / "unpacked"

    @property
    def pdf(self) -> Path:
        return self.root / "pdf"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def ensure(self) -> PackagePaths:
        for folder in (self.original, self.unpacked, self.pdf, self.reports, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def source_for(self, document_id: uuid.UUID, filename: str) -> Path:
        return self.unpacked / f"{document_id}{PurePosixPath(filename).suffix.lower()}"

    def pdf_for(self, document_id: uuid.UUID) -> Path:
        return self.pdf / f"{document_id}.pdf"

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def package_paths(packages_dir: Path, package_id: uuid.UUID) -> PackagePaths:
    return PackagePaths(packages_dir / str(package_id))


def safe_filename(name: str, default: str = "package") -> str:
    """Имя для диска из имени, присланного пользователем: без путей и служебных символов."""
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return cleaned[:150] or default
