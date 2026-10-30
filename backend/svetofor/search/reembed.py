"""Пересчёт эмбеддингов после смены модели: `python -m svetofor.search.reembed` (make reembed).

Если размерность новой модели (EMBED_DIM) отличается от столбца в БД, тип столбца меняется,
векторы обнуляются, индекс HNSW пересоздаётся. Затем векторы считаются заново для всех
фрагментов. Пока пересчёт идёт, поиск работает по тексту.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from svetofor.core.config import get_settings
from svetofor.core.logging import configure_logging, get_logger
from svetofor.db.models import Chunk
from svetofor.db.session import get_sessionmaker
from svetofor.search.embeddings import Embedder

log = get_logger(__name__)
BATCH = 64


def column_dimension(db: Session) -> int | None:
    """Размерность столбца chunks.embedding (vector(N)) из каталога PostgreSQL."""
    value = db.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
    ).scalar()
    return int(value) if value is not None and value > 0 else None


def ensure_dimension(db: Session, dim: int) -> bool:
    """Подогнать столбец под размерность модели. True — столбец пересоздан, векторы сброшены."""
    if column_dimension(db) == dim:
        return False
    db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
    db.execute(
        text(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({int(dim)}) USING NULL")
    )
    db.execute(
        text(
            "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
            "USING hnsw (embedding vector_cosine_ops)"
        )
    )
    db.commit()
    return True


def reembed(session_factory: Callable[[], Session], embedder: Embedder) -> int:
    with session_factory() as db:
        ensure_dimension(db, embedder.dim)
        ids = list(db.scalars(select(Chunk.id).order_by(Chunk.document_id, Chunk.order_index)))
    done = 0
    for start in range(0, len(ids), BATCH):
        with session_factory() as db:
            chunks = list(db.scalars(select(Chunk).where(Chunk.id.in_(ids[start : start + BATCH]))))
            vectors = embedder.embed_passages([f"{c.section_path}: {c.text}" for c in chunks])
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = vector
            db.commit()
        done += len(chunks)
        log.info("reembed_progress", done=done, total=len(ids))
    return done


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    embedder = Embedder(url=settings.embed_url, model=settings.embed_model, dim=settings.embed_dim)
    count = reembed(get_sessionmaker(), embedder)
    model = f"модель {settings.embed_model}, размерность {settings.embed_dim}"
    print(f"Пересчитано фрагментов: {count} ({model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
