"""Очистка неудачной попытки не удаляет архив повторного запуска."""

import uuid
from datetime import UTC, datetime, timedelta

from svetofor.core.config import Settings
from svetofor.db.portfolio import PortfolioOperation
from svetofor.portfolios.edit_files import attempt_folder, cleanup_attempt


def test_cleanup_only_removes_failed_attempt(tmp_path):
    settings = Settings(data_dir=tmp_path)
    identity = uuid.uuid4()
    now = datetime.now(UTC)
    failed = PortfolioOperation(id=identity, started_at=now, payload={"sources": []})
    retried = PortfolioOperation(
        id=identity, started_at=now + timedelta(seconds=1), payload={"sources": []}
    )
    old_folder, new_folder = attempt_folder(failed, settings), attempt_folder(retried, settings)
    for folder in (old_folder, new_folder):
        folder.mkdir(parents=True)
        (folder / "bundle.zip").write_bytes(b"archive")
        (folder / "interrupted.tmp").write_bytes(b"partial")
    cleanup_attempt(failed, settings)
    assert not old_folder.exists()
    assert (new_folder / "bundle.zip").read_bytes() == b"archive"
    assert (new_folder / "interrupted.tmp").exists()
