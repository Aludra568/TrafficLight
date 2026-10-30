"""Эмбеддинги через сервис TEI.

Пакеты по 32 текста (предел TEI по умолчанию), кэш по sha256 текста и имени модели —
повторная загрузка того же пакета не пересчитывает векторы. Пока модель в контейнере
загружается, TEI отвечает 503 — ждём с повторами, а не роняем проверку.
"""

from __future__ import annotations

import hashlib
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from redis.exceptions import RedisError

from svetofor.core.logging import get_logger

log = get_logger(__name__)
BATCH_SIZE = 32
_RETRY_STATUSES = {429, 502, 503, 504}


class EmbeddingError(Exception):
    """Сервис эмбеддингов недоступен или ответил не то. Сообщение — для пользователя."""


@dataclass(frozen=True)
class ModelProfile:
    """Как модель ждёт тексты: семейство e5 обучено с префиксами «query:» и «passage:»."""

    query_prefix: str = ""
    passage_prefix: str = ""


def model_profile(model: str) -> ModelProfile:
    if "e5" in model.lower():
        return ModelProfile("query: ", "passage: ")
    return ModelProfile()


class EmbeddingCache(Protocol):
    def get_many(self, keys: Sequence[str]) -> list[list[float] | None]: ...

    def put_many(self, items: dict[str, list[float]]) -> None: ...


class MemoryEmbeddingCache:
    def __init__(self) -> None:
        self.items: dict[str, list[float]] = {}

    def get_many(self, keys: Sequence[str]) -> list[list[float] | None]:
        return [self.items.get(k) for k in keys]

    def put_many(self, items: dict[str, list[float]]) -> None:
        self.items.update(items)


class RedisEmbeddingCache:
    """Векторы в Redis компактно (float32). Сбой Redis — просто пересчёт, не ошибка."""

    def __init__(self, client: Any, ttl_s: int = 30 * 24 * 3600, prefix: str = "emb:") -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._prefix = prefix

    def get_many(self, keys: Sequence[str]) -> list[list[float] | None]:
        if not keys:
            return []
        try:
            raw = self._client.mget([self._prefix + k for k in keys])
        except RedisError:
            log.warning("embedding_cache_unavailable", op="get")
            return [None] * len(keys)
        return [None if r is None else list(struct.unpack(f"<{len(r) // 4}f", r)) for r in raw]

    def put_many(self, items: dict[str, list[float]]) -> None:
        try:
            pipe = self._client.pipeline(transaction=False)
            for key, vector in items.items():
                pipe.set(
                    self._prefix + key, struct.pack(f"<{len(vector)}f", *vector), ex=self._ttl_s
                )
            pipe.execute()
        except RedisError:
            log.warning("embedding_cache_unavailable", op="put")


def text_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


class Embedder:
    def __init__(
        self,
        *,
        url: str,
        model: str,
        dim: int,
        cache: EmbeddingCache | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 60,
        retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url.rstrip("/") + "/embed"
        self.model = model
        self.dim = dim
        self.profile = model_profile(model)
        self._cache = cache
        self._client = client or httpx.Client(timeout=timeout_s)
        self._retries = retries
        self._sleep = sleep

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self.profile.passage_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.profile.query_prefix + text])[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Несколько запросов одним обращением к сервису (по запросу на правило группы)."""
        return self._embed([self.profile.query_prefix + t for t in texts])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        keys = [text_key(self.model, t) for t in texts]
        cached: list[list[float] | None] = (
            self._cache.get_many(keys) if self._cache else [None] * len(texts)
        )
        missing = [i for i, vector in enumerate(cached) if vector is None]
        fresh: dict[str, list[float]] = {}
        for start in range(0, len(missing), BATCH_SIZE):
            batch = missing[start : start + BATCH_SIZE]
            vectors = self._request([texts[i] for i in batch])
            for i, vector in zip(batch, vectors, strict=True):
                cached[i] = vector
                fresh[keys[i]] = vector
        if fresh and self._cache:
            self._cache.put_many(fresh)
        return [v for v in cached if v is not None]

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload = {"inputs": texts, "truncate": True, "normalize": True}
        for attempt in range(self._retries + 1):
            try:
                response = self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise EmbeddingError("Сервис поиска (эмбеддинги) недоступен") from exc
            else:
                if response.status_code not in _RETRY_STATUSES:
                    return self._parse(response, len(texts))
                if attempt == self._retries:
                    raise EmbeddingError("Сервис поиска (эмбеддинги) перегружен или загружается")
            self._sleep(min(2.0**attempt, 30.0))
        raise AssertionError("недостижимо")  # pragma: no cover

    def _parse(self, response: httpx.Response, expected: int) -> list[list[float]]:
        if response.status_code != 200:
            raise EmbeddingError(f"Сервис эмбеддингов ответил ошибкой {response.status_code}")
        vectors = response.json()
        if not isinstance(vectors, list) or len(vectors) != expected:
            raise EmbeddingError("Сервис эмбеддингов вернул неожиданный ответ")
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"Размерность векторов модели ({len(vector)}) не совпадает с EMBED_DIM "
                    f"({self.dim}): после смены модели нужен пересчёт (make reembed)"
                )
        return [[float(x) for x in vector] for vector in vectors]
