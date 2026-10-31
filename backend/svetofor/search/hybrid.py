"""Гибридный поиск фрагментов.

Вектор (pgvector, косинус) находит перефразировки, полнотекст PostgreSQL (`russian`) — точные
юридические термины («односторонний отказ», «1/300»). Списки сливаются Reciprocal Rank Fusion:
фрагмент, найденный обоими способами, поднимается выше. Затем к найденному добавляются соседние
фрагменты того же раздела — в пределах бюджета токенов промпта.

Несколько правил в одном вызове модели — несколько запросов: у каждого правила свои списки
(вектор и полнотекст), и RRF сливает их все. Лучшие фрагменты каждого правила получают
одинаково высокий вес, поэтому фрагмент редкого правила не вытесняется фрагментами частого
(при одном общем запросе так и происходило).
"""

from __future__ import annotations

import uuid
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from svetofor.core.logging import get_logger
from svetofor.db.enums import DocType
from svetofor.db.models import Chunk, Document
from svetofor.rules.verify import normalize
from svetofor.search.embeddings import Embedder, EmbeddingError

log = get_logger(__name__)
RRF_K = 60
PER_METHOD = 20
QUOTA = 2  # первые места каждого списка — вне очереди RRF (1 и 2 дали одно и то же, 3 — хуже)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    score: float
    ranks: dict[str, int] = field(default_factory=dict)  # место в каждом списке, с 1


def rrf_merge[K: Hashable](
    rankings: Mapping[str, Sequence[K]], k: int = RRF_K
) -> list[tuple[K, float, dict[str, int]]]:
    """Reciprocal Rank Fusion: score = Σ 1 / (k + место). При равенстве — порядок появления."""
    scores: dict[K, float] = {}
    ranks: dict[K, dict[str, int]] = {}
    for method, ranking in rankings.items():
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
            ranks.setdefault(item, {})[method] = position
    order = {item: i for i, item in enumerate(scores)}
    merged = sorted(scores, key=lambda item: (-scores[item], order[item]))
    return [(item, scores[item], ranks[item]) for item in merged]


def quota_first[K: Hashable](
    merged: Sequence[tuple[K, float, dict[str, int]]], quota: int
) -> list[tuple[K, float, dict[str, int]]]:
    """Результат RRF: сначала то, что хотя бы в одном списке на месте ≤ `quota` (по лучшему
    месту, при равенстве — порядок RRF), затем остальное в порядке RRF."""
    head = [m for m in merged if min(m[2].values()) <= quota]
    head.sort(key=lambda m: min(m[2].values()))
    return head + [m for m in merged if min(m[2].values()) > quota]


def fts_query(hints: Sequence[str]) -> str:
    """Подсказки правил → запрос websearch_to_tsquery: фразы в кавычках, между ними «or»."""
    parts: list[str] = []
    for hint in hints:
        cleaned = normalize(hint).replace('"', " ").replace("-", " ")
        words = cleaned.split()
        if not words:
            continue
        parts.append(f'"{" ".join(words)}"' if len(words) > 1 else words[0])
    return " or ".join(parts)


@dataclass(frozen=True)
class Query:
    text: str  # для вектора: подсказки и вопрос правила
    hints: tuple[str, ...]  # для полнотекста: точные формулировки


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    vector_used: bool  # False — поиск шёл только по тексту (сервис эмбеддингов недоступен)


class HybridSearch:
    def __init__(self, session: Session, embedder: Embedder | None) -> None:
        self._session = session
        self._embedder = embedder

    def _scope(self, package_id: uuid.UUID, doc_types: Sequence[DocType]) -> Select[uuid.UUID]:
        statement = select(Chunk.id).join(Document, Document.id == Chunk.document_id)
        statement = statement.where(Document.package_id == package_id)
        if doc_types:
            statement = statement.where(Document.doc_type.in_(list(doc_types)))
        return statement

    def _query_vectors(self, queries: Sequence[Query]) -> list[list[float] | None] | None:
        """Векторы запросов одним обращением. None — сервис эмбеддингов недоступен."""
        texts = [q.text for q in queries if q.text.strip()]
        if self._embedder is None or not texts:
            return None
        try:
            vectors = iter(self._embedder.embed_queries(texts))
        except EmbeddingError:
            log.warning("vector_search_unavailable")
            return None
        return [next(vectors) if q.text.strip() else None for q in queries]

    def _vector_ranking(
        self,
        package_id: uuid.UUID,
        doc_types: Sequence[DocType],
        vector: list[float],
        limit: int,
    ) -> list[uuid.UUID]:
        statement = (
            self._scope(package_id, doc_types)
            .where(Chunk.embedding.is_not(None))
            .order_by(Chunk.embedding.cosine_distance(vector), Chunk.order_index)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def _text_ranking(
        self, package_id: uuid.UUID, doc_types: Sequence[DocType], hints: Sequence[str], limit: int
    ) -> list[uuid.UUID]:
        query_text = fts_query(hints)
        if not query_text:
            return []
        tsquery = func.websearch_to_tsquery("russian", query_text)
        statement = (
            self._scope(package_id, doc_types)
            .where(Chunk.tsv.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(Chunk.tsv, tsquery).desc(), Chunk.order_index)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def search(
        self,
        *,
        package_id: uuid.UUID,
        doc_types: Sequence[DocType],
        queries: Sequence[Query],
        limit: int | None = None,
        per_method: int = PER_METHOD,
        quota: int = QUOTA,
    ) -> SearchResult:
        """Списки по каждому запросу (вектор + полнотекст) → один RRF.

        `quota`: первые места каждого списка идут раньше остальных, а сколько фрагментов
        поместится, решает бюджет токенов в `expand`. Так у каждого правила есть свои лучшие
        фрагменты: без квоты RRF поднимал фрагменты, средне подходящие многим правилам, выше
        лучшего фрагмента одного правила, и тот не попадал в отрезанный топ.
        `quota=0` — прежний порядок: RRF и топ `max(8, 3 × число запросов)`.
        """
        rankings: dict[str, list[uuid.UUID]] = {}
        vectors = self._query_vectors(queries)
        for index, query in enumerate(queries):
            vector = vectors[index] if vectors else None
            if vector is not None:
                rankings[f"vector:{index}"] = self._vector_ranking(
                    package_id, doc_types, vector, per_method
                )
            rankings[f"text:{index}"] = self._text_ranking(
                package_id, doc_types, query.hints, per_method
            )
        merged = rrf_merge(rankings)
        if quota:
            merged = quota_first(merged, quota)
            top = limit if limit is not None else len(merged)
        else:
            top = limit if limit is not None else max(8, 3 * len(queries))
        merged = merged[:top]
        hits = [SearchHit(chunk_id, score, ranks) for chunk_id, score, ranks in merged]
        return SearchResult(hits, vector_used=vectors is not None)

    def expand(self, hits: Sequence[SearchHit], budget_tokens: int = 6000) -> list[Chunk]:
        """Найденные фрагменты + соседние в том же разделе, пока хватает бюджета токенов.

        Порядок для промпта — по документам и положению в документе: так модель читает
        текст связно, а не вперемешку.
        """
        if not hits:
            return []
        found = {
            c.id: c
            for c in self._session.scalars(
                select(Chunk).where(Chunk.id.in_([h.chunk_id for h in hits]))
            )
        }
        chosen: dict[uuid.UUID, Chunk] = {}
        used = 0
        for hit in hits:
            chunk = found.get(hit.chunk_id)
            if chunk is not None and (not chosen or used + chunk.token_count <= budget_tokens):
                chosen[chunk.id] = chunk
                used += chunk.token_count
        for hit in hits:
            chunk = found.get(hit.chunk_id)
            if chunk is None or chunk.id not in chosen:
                continue
            for neighbour in self._neighbours(chunk):
                if neighbour.id not in chosen and used + neighbour.token_count <= budget_tokens:
                    chosen[neighbour.id] = neighbour
                    used += neighbour.token_count
        doc_rank: dict[uuid.UUID, int] = {}
        for hit in hits:
            chunk = found.get(hit.chunk_id)
            if chunk is not None:
                doc_rank.setdefault(chunk.document_id, len(doc_rank))
        return sorted(
            chosen.values(), key=lambda c: (doc_rank.get(c.document_id, 0), c.order_index)
        )

    def _neighbours(self, chunk: Chunk) -> list[Chunk]:
        statement = select(Chunk).where(
            Chunk.document_id == chunk.document_id,
            Chunk.order_index.in_([chunk.order_index - 1, chunk.order_index + 1]),
        )
        if chunk.section_id is not None:
            statement = statement.where(Chunk.section_id == chunk.section_id)
        return list(self._session.scalars(statement.order_by(Chunk.order_index)))
