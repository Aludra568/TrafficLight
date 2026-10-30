"""Кэш ответов LLM (память, файл, Redis, слои) и ограничитель частоты."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from svetofor.llm.cache import CachedReply, JsonlCache, LayeredCache, MemoryCache, RedisCache
from svetofor.llm.errors import LLMUnavailableError
from svetofor.llm.ratelimit import LocalStore, RateLimiter, RedisStore

REPLY = CachedReply(
    "check_group", "m", "check_group.v1", {"verdict": "ok", "quote": "«текст»"}, 10, 5
)


# ─── Кэш ───


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cassettes" / "demo.jsonl"
    cache = JsonlCache(path)
    cache.put("k1", REPLY)
    reopened = JsonlCache(path)
    assert reopened.get("k1") == REPLY and reopened.get("missing") is None
    assert "«текст»" in path.read_text(encoding="utf-8")  # кириллица без \\u-экранирования


def test_jsonl_last_write_wins(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    cache = JsonlCache(path)
    cache.put("k", REPLY)
    newer = CachedReply(**{**REPLY.__dict__, "response": {"verdict": "violation"}})
    cache.put("k", newer)
    assert JsonlCache(path).get("k") == newer


def test_jsonl_same_reply_written_once(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    cache = JsonlCache(path)
    cache.put("k", REPLY)
    cache.put("k", REPLY)  # повторный вызов с тем же ответом — например, из кэша
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("payload", [b"{not json", b'{"unexpected": 1}', b"[]"])
def test_redis_cache_damaged_entry_is_a_miss(payload: bytes) -> None:
    client = fakeredis.FakeRedis()
    client.set("llm:cache:k", payload)
    assert RedisCache(client).get("k") is None


def test_redis_cache_with_ttl() -> None:
    client = fakeredis.FakeRedis()
    cache = RedisCache(client, ttl_s=100)
    cache.put("k", REPLY)
    assert cache.get("k") == REPLY
    assert 0 < client.ttl("llm:cache:k") <= 100


class BrokenRedis:
    def get(self, *_: Any) -> None:
        raise RedisConnectionError("down")

    def set(self, *_: Any, **__: Any) -> None:
        raise RedisConnectionError("down")


def test_redis_failure_is_not_fatal() -> None:
    cache = RedisCache(BrokenRedis())
    cache.put("k", REPLY)
    assert cache.get("k") is None


def test_db_cache_failure_is_not_fatal() -> None:
    """Ответ модели уже получен — сбой записи в кэш не должен его выбросить."""
    from svetofor.llm.cache import DbCache

    def broken() -> Any:
        raise RuntimeError("БД недоступна")

    cache = DbCache(broken)
    assert cache.get("k") is None
    cache.put("k", REPLY)  # не бросает


def test_layered_backfills_faster_layers() -> None:
    hot, cold = MemoryCache(), MemoryCache()
    layered = LayeredCache(hot, cold)
    cold.put("k", REPLY)
    assert layered.get("k") == REPLY
    assert hot.get("k") == REPLY
    layered.put("k2", REPLY)
    assert hot.get("k2") == REPLY and cold.get("k2") == REPLY


@pytest.mark.pg
def test_db_cache_upsert(migrated_engine: Any) -> None:
    from sqlalchemy import delete
    from sqlalchemy.orm import sessionmaker

    from svetofor.db.models import LLMCacheEntry
    from svetofor.llm.cache import DbCache

    factory = sessionmaker(bind=migrated_engine)
    cache = DbCache(factory)
    try:
        assert cache.get("db-key") is None
        cache.put("db-key", REPLY)
        assert cache.get("db-key") == REPLY
        newer = CachedReply(**{**REPLY.__dict__, "tokens_out": 99})
        cache.put("db-key", newer)  # повторная запись не падает на первичном ключе
        assert cache.get("db-key") == newer
    finally:
        with factory() as session:
            session.execute(delete(LLMCacheEntry).where(LLMCacheEntry.key == "db-key"))
            session.commit()


# ─── Ограничитель частоты ───


class FakeClock:
    def __init__(self, start: float = 60 * 16_667 + 20) -> None:  # 20-я секунда минуты
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(params=["local", "redis"])
def store(request: pytest.FixtureRequest) -> Any:
    return LocalStore() if request.param == "local" else RedisStore(fakeredis.FakeRedis())


def limiter(store: Any, clock: FakeClock, **kw: Any) -> RateLimiter:
    return RateLimiter(
        store, rpm=kw.pop("rpm", 2), tpm=kw.pop("tpm", 1000), clock=clock, sleep=clock.sleep, **kw
    )


def test_requests_per_minute(store: Any) -> None:
    clock = FakeClock()
    rl = limiter(store, clock)
    rl.acquire(10)
    rl.acquire(10)
    assert clock.sleeps == []
    rl.acquire(10)  # третий запрос в минуту — ждём следующую
    assert len(clock.sleeps) == 1 and 40 <= clock.sleeps[0] <= 41


def test_tokens_per_minute(store: Any) -> None:
    clock = FakeClock()
    rl = limiter(store, clock, rpm=100, tpm=1000)
    rl.acquire(600)
    rl.acquire(600)
    assert len(clock.sleeps) == 1


def test_single_huge_request_passes_when_first(store: Any) -> None:
    clock = FakeClock()
    rl = limiter(store, clock, tpm=1000)
    assert rl.acquire(5000) is not None and clock.sleeps == []


def test_settle_returns_unused_tokens(store: Any) -> None:
    clock = FakeClock()
    rl = limiter(store, clock, rpm=100, tpm=1000)
    reservation = rl.acquire(900)  # резерв по оценке
    rl.settle(reservation, 100)  # реально потрачено меньше
    rl.acquire(800)
    assert clock.sleeps == []


def test_wait_limit(store: Any) -> None:
    clock = FakeClock()
    rl = limiter(store, clock, rpm=1, max_wait_s=10)
    rl.acquire(1)
    with pytest.raises(LLMUnavailableError, match="LLM_RPM"):
        rl.acquire(1)


class BrokenPipeline:
    def pipeline(self, **_: Any) -> BrokenPipeline:
        return self

    def __getattr__(self, name: str) -> Any:
        def fail(*_: Any, **__: Any) -> None:
            raise RedisConnectionError("down")

        return fail


def test_redis_down_means_no_limit() -> None:
    clock = FakeClock()
    rl = RateLimiter(RedisStore(BrokenPipeline()), rpm=1, tpm=1, clock=clock, sleep=clock.sleep)
    assert rl.acquire(10) is None
    rl.settle(None, 5)
