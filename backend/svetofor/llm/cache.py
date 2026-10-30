"""Кэш ответов LLM.

Ключ — sha256 от модели, сообщений, версии промпта и отпечатка схемы ответа: изменили промпт
или схему — старые ответы перестают подходить сами. Слои: Redis (горячий), PostgreSQL
(постоянный), JSONL-файл («кассеты» для тестов и демо-кэш в репозитории — демо без ключа).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from svetofor.core.logging import get_logger
from svetofor.db.models import LLMCacheEntry
from svetofor.llm.backend import Message

log = get_logger(__name__)


@dataclass(frozen=True)
class CachedReply:
    task: str
    model: str
    prompt_version: str
    response: dict[str, Any]
    tokens_in: int = 0
    tokens_out: int = 0


class LLMCache(Protocol):
    def get(self, key: str) -> CachedReply | None: ...

    def put(self, key: str, reply: CachedReply) -> None: ...


def schema_fingerprint(schema: type[BaseModel]) -> str:
    raw = json.dumps(schema.model_json_schema(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def cache_key(
    model: str, messages: Sequence[Message], prompt_version: str, schema: type[BaseModel]
) -> str:
    payload = {
        "model": model,
        "messages": [[m.role, m.content] for m in messages],
        "prompt_version": prompt_version,
        "schema": schema_fingerprint(schema),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class MemoryCache:
    def __init__(self) -> None:
        self._items: dict[str, CachedReply] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> CachedReply | None:
        with self._lock:
            return self._items.get(key)

    def put(self, key: str, reply: CachedReply) -> None:
        with self._lock:
            self._items[key] = reply

    def __len__(self) -> int:
        return len(self._items)


class JsonlCache:
    """Файл «ключ + ответ» построчно. Читается целиком при открытии, запись — дописыванием."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._memory = MemoryCache()
        self._lock = threading.Lock()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self._memory.put(record.pop("key"), CachedReply(**record))

    def get(self, key: str) -> CachedReply | None:
        return self._memory.get(key)

    def put(self, key: str, reply: CachedReply) -> None:
        if self._memory.get(key) == reply:
            return  # тот же ответ из кэша при повторном вызове — строку не дублируем
        self._memory.put(key, reply)
        line = json.dumps({"key": key, **asdict(reply)}, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(line + "\n")

    def __len__(self) -> int:
        return len(self._memory)


class ReadOnlyCache:
    """Слой, из которого только читают: кассета демо в репозитории не дописывается рабочими
    вызовами (иначе каждый ответ модели попадал бы в samples/)."""

    def __init__(self, inner: LLMCache) -> None:
        self.inner = inner

    def get(self, key: str) -> CachedReply | None:
        return self.inner.get(key)

    def put(self, key: str, reply: CachedReply) -> None:
        return None


class DbCache:
    """Постоянный слой: таблица `llm_cache`. Своя короткая сессия на каждую операцию."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> CachedReply | None:
        try:
            return self._get(key)
        except Exception:  # кэш вспомогательный: без него просто спросим модель
            log.warning("llm_cache_db_unavailable", op="get", exc_info=True)
            return None

    def _get(self, key: str) -> CachedReply | None:
        with self._session_factory() as session:
            entry = session.get(LLMCacheEntry, key)
            if entry is None:
                return None
            return CachedReply(
                task=entry.task,
                model=entry.model,
                prompt_version=entry.prompt_version,
                response=entry.response,
                tokens_in=entry.tokens_in,
                tokens_out=entry.tokens_out,
            )

    def put(self, key: str, reply: CachedReply) -> None:
        values = {"key": key, **asdict(reply)}
        statement = insert(LLMCacheEntry).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[LLMCacheEntry.key],
            set_={k: statement.excluded[k] for k in values if k != "key"},
        )
        try:
            with self._session_factory() as session:
                session.execute(statement)
                session.commit()
        except Exception:  # ответ модели уже получен — потеря записи в кэш не повод его выбросить
            log.warning("llm_cache_db_unavailable", op="put", exc_info=True)


class RedisCache:
    """Горячий слой: ответы в Redis с ограниченным сроком жизни.

    Сбой Redis не роняет вызов модели: слой просто пропускается (ответ есть в PostgreSQL).
    """

    def __init__(self, client: Any, ttl_s: int = 7 * 24 * 3600, prefix: str = "llm:cache:") -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._prefix = prefix

    def get(self, key: str) -> CachedReply | None:
        try:
            raw = self._client.get(self._prefix + key)
        except RedisError:
            log.warning("llm_cache_redis_unavailable", op="get")
            return None
        if raw is None:
            return None
        try:
            return CachedReply(**json.loads(raw))
        except (ValueError, TypeError):  # испорченная или старого формата запись — спросим заново
            log.warning("llm_cache_redis_invalid")
            return None

    def put(self, key: str, reply: CachedReply) -> None:
        payload = json.dumps(asdict(reply), ensure_ascii=False)
        try:
            self._client.set(self._prefix + key, payload, ex=self._ttl_s)
        except RedisError:
            log.warning("llm_cache_redis_unavailable", op="put")


class LayeredCache:
    """Слои от быстрого к медленному. Попадание в медленный слой поднимается в быстрые."""

    def __init__(self, *layers: LLMCache) -> None:
        self._layers = layers

    def get(self, key: str) -> CachedReply | None:
        for index, layer in enumerate(self._layers):
            reply = layer.get(key)
            if reply is not None:
                for faster in self._layers[:index]:
                    faster.put(key, reply)
                return reply
        return None

    def put(self, key: str, reply: CachedReply) -> None:
        for layer in self._layers:
            layer.put(key, reply)
