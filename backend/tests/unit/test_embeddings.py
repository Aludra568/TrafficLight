"""Клиент TEI: пакеты, кэш, повторы при загрузке модели, проверка размерности, префиксы e5."""

from __future__ import annotations

import json

import fakeredis
import httpx
import pytest
import respx

from svetofor.search.embeddings import (
    BATCH_SIZE,
    Embedder,
    EmbeddingError,
    MemoryEmbeddingCache,
    RedisEmbeddingCache,
    model_profile,
)
from svetofor.search.hybrid import fts_query, rrf_merge

URL = "http://embedder:80"
DIM = 4


def vectors_for(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["inputs"]
    return httpx.Response(200, json=[[float(len(t)), 0.0, 0.0, 1.0] for t in inputs])


def embedder(**kw: object) -> Embedder:
    return Embedder(
        url=URL, model=str(kw.pop("model", "BAAI/bge-m3")), dim=DIM, sleep=lambda _: None, **kw
    )  # type: ignore[arg-type]


@respx.mock
def test_batches_and_order() -> None:
    route = respx.post(f"{URL}/embed").mock(side_effect=vectors_for)
    texts = [f"текст {i}" * (i + 1) for i in range(BATCH_SIZE + 5)]
    vectors = embedder().embed_passages(texts)
    assert route.call_count == 2
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]
    payload = json.loads(route.calls[0].request.content)
    assert payload["truncate"] is True and len(payload["inputs"]) == BATCH_SIZE


@respx.mock
def test_cache_avoids_recompute() -> None:
    route = respx.post(f"{URL}/embed").mock(side_effect=vectors_for)
    cache = MemoryEmbeddingCache()
    first = embedder(cache=cache).embed_passages(["а", "бб"])
    second = embedder(cache=cache).embed_passages(["бб", "а", "ввв"])
    assert route.call_count == 2
    assert json.loads(route.calls[1].request.content)["inputs"] == ["ввв"]  # только новое
    assert second[:2] == [first[1], first[0]]


@respx.mock
def test_deduplicates_texts_in_same_call_without_cache() -> None:
    route = respx.post(f"{URL}/embed").mock(side_effect=vectors_for)
    texts = ["а-текст", "б-текст", "а-текст"]
    vectors = embedder().embed_passages(texts)
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["inputs"] == ["а-текст", "б-текст"]
    assert len(vectors) == 3
    assert vectors[0] == vectors[2]


@respx.mock
def test_deduplicates_forty_identical_texts() -> None:
    route = respx.post(f"{URL}/embed").mock(side_effect=vectors_for)
    texts = ["одинаковый текст"] * 40
    vectors = embedder().embed_passages(texts)
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["inputs"] == ["одинаковый текст"]
    assert len(vectors) == 40
    assert all(v == vectors[0] for v in vectors)


def test_redis_cache_roundtrip() -> None:
    cache = RedisEmbeddingCache(fakeredis.FakeRedis())
    cache.put_many({"k": [0.5, -1.25, 3.0]})
    assert cache.get_many(["k", "missing"]) == [[0.5, -1.25, 3.0], None]


@respx.mock
def test_retries_while_model_loads() -> None:
    route = respx.post(f"{URL}/embed").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("refused"),
            httpx.Response(200, json=[[1.0] * DIM]),
        ]
    )
    assert embedder().embed_query("запрос") == [1.0] * DIM
    assert route.call_count == 3


@respx.mock
def test_gives_up_with_clear_error() -> None:
    respx.post(f"{URL}/embed").mock(return_value=httpx.Response(503))
    with pytest.raises(EmbeddingError, match="перегружен или загружается"):
        embedder(retries=2).embed_query("запрос")


@respx.mock
def test_dimension_mismatch() -> None:
    respx.post(f"{URL}/embed").mock(return_value=httpx.Response(200, json=[[1.0, 2.0]]))
    with pytest.raises(EmbeddingError, match="make reembed"):
        embedder().embed_query("запрос")


@respx.mock
def test_e5_prefixes() -> None:
    route = respx.post(f"{URL}/embed").mock(side_effect=vectors_for)
    e5 = embedder(model="intfloat/multilingual-e5-large")
    e5.embed_query("неустойка")
    e5.embed_passages(["пункт"])
    assert json.loads(route.calls[0].request.content)["inputs"] == ["query: неустойка"]
    assert json.loads(route.calls[1].request.content)["inputs"] == ["passage: пункт"]
    assert model_profile("BAAI/bge-m3").query_prefix == ""


def test_rrf_merge() -> None:
    merged = rrf_merge({"vector": ["a", "b", "c"], "text": ["c", "d"]})
    assert [item for item, _, _ in merged][:2] == ["c", "a"]  # найден обоими — выше
    ranks = {item: r for item, _, r in merged}
    assert ranks["c"] == {"vector": 3, "text": 1} and ranks["d"] == {"text": 2}
    assert rrf_merge({}) == []


def test_fts_query() -> None:
    assert fts_query(["односторонний отказ", "1/300", "Пеня", "  "]) == (
        '"односторонний отказ" or 1/300 or пеня'
    )
    assert fts_query(['"кавычки"  и  ё-мобиль']) == '"кавычки и е мобиль"'
    assert fts_query([]) == ""
