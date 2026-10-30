"""Пачки для сервиса эмбеддингов: по длине, мелкие, без потерь и без изменения текста."""

from __future__ import annotations

from svetofor.db.models import Chunk
from svetofor.pipeline.orchestrator import EMBED_BATCH, embedding_batches, embedding_input


def _chunk(index: int, length: int) -> Chunk:
    return Chunk(section_path=f"Контракт › {index}", text="а" * length, order_index=index)


def test_batches_are_short_and_ordered_by_length() -> None:
    chunks = [_chunk(i, length) for i, length in enumerate([700, 20, 350, 5, 690, 40] * 5)]
    batches = embedding_batches(chunks)
    assert all(0 < len(b) <= EMBED_BATCH for b in batches)
    flat = [c for b in batches for c in b]
    lengths = [len(embedding_input(c)) for c in flat]
    assert lengths == sorted(lengths)
    assert sorted(id(c) for c in flat) == sorted(id(c) for c in chunks)  # каждый ровно один раз


def test_input_text_is_unchanged() -> None:
    """Порядок меняется, текст — нет: векторы те же, что и до ускорения."""
    chunk = _chunk(1, 3)
    assert embedding_input(chunk) == "Контракт › 1: ааа"
