"""Размерность векторов фрагментов — из настроек (EMBED_DIM)

Первая миграция создала vector(1024) под BAAI/bge-m3. Модель по умолчанию теперь
intfloat/multilingual-e5-small (384): вдвое меньше памяти и вдвое быстрее на CPU при том же
качестве поиска (замер 31.10.2026). Столбец подгоняется под EMBED_DIM; векторы другой модели
несовместимы и сбрасываются — пересчёт старых фрагментов: make reembed.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31 11:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from svetofor.core.config import get_settings

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGINAL_DIM = 1024


def _column_dim() -> int | None:
    value = op.get_bind().execute(
        sa.text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
    ).scalar()
    return int(value) if value is not None and value > 0 else None


def _set_dim(dim: int) -> None:
    if _column_dim() == dim:
        return
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({int(dim)}) USING NULL")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def upgrade() -> None:
    _set_dim(get_settings().embed_dim)


def downgrade() -> None:
    _set_dim(_ORIGINAL_DIM)
