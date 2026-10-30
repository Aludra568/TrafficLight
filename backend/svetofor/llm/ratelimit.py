"""Ограничитель частоты запросов к LLM.

Лимиты провайдеров формулируются «N запросов и T токенов в минуту», поэтому считаем ровно так:
счётчики на текущую минуту в Redis, общие для всех воркеров. Резерв делается до вызова
(оценка входа + max_tokens), после вызова уточняется по фактическому расходу.
Если места в текущей минуте нет — резерв откатывается и воркер ждёт следующую минуту.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from redis.exceptions import RedisError

from svetofor.core.logging import get_logger
from svetofor.llm.errors import LLMUnavailableError

log = get_logger(__name__)
WINDOW_S = 60


@dataclass(frozen=True)
class Reservation:
    window: int
    tokens: int


class _WindowStore(Protocol):
    def add(self, window: int, requests: int, tokens: int) -> tuple[int, int]:
        """Прибавить к счётчикам окна и вернуть новые значения (запросы, токены)."""
        ...


class LocalStore:
    """Счётчики в памяти процесса — для запусков без Redis (оценка качества, разработка)."""

    def __init__(self) -> None:
        self._counts: dict[int, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def add(self, window: int, requests: int, tokens: int) -> tuple[int, int]:
        with self._lock:
            for old in [w for w in self._counts if w < window - 1]:
                del self._counts[old]
            current = self._counts.get(window, (0, 0))
            updated = (current[0] + requests, current[1] + tokens)
            self._counts[window] = updated
            return updated


class RedisStore:
    def __init__(self, client: Any, prefix: str = "llm:rate:") -> None:
        self._client = client
        self._prefix = prefix

    def add(self, window: int, requests: int, tokens: int) -> tuple[int, int]:
        req_key, tok_key = f"{self._prefix}{window}:req", f"{self._prefix}{window}:tok"
        pipe = self._client.pipeline(transaction=True)
        pipe.incrby(req_key, requests)
        pipe.incrby(tok_key, tokens)
        pipe.expire(req_key, WINDOW_S * 2)
        pipe.expire(tok_key, WINDOW_S * 2)
        req_total, tok_total, _, _ = pipe.execute()
        return int(req_total), int(tok_total)


class RateLimiter:
    def __init__(
        self,
        store: _WindowStore,
        *,
        rpm: int,
        tpm: int,
        max_wait_s: float = 600,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self.rpm = rpm
        self.tpm = tpm
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._sleep = sleep

    def acquire(self, tokens: int) -> Reservation | None:
        """Занять место под запрос. None — счётчики недоступны (Redis), идём без ограничения."""
        deadline = self._clock() + self._max_wait_s
        while True:
            now = self._clock()
            window = int(now // WINDOW_S)
            try:
                requests, used = self._store.add(window, 1, tokens)
            except RedisError:
                log.warning("llm_ratelimit_unavailable")
                return None
            # один запрос больше минутного лимита токенов всё равно пропускаем, если он первый
            if requests <= self.rpm and (used <= self.tpm or used == tokens):
                return Reservation(window, tokens)
            self._store.add(window, -1, -tokens)
            wait = (window + 1) * WINDOW_S - now + random.uniform(0, 1)  # noqa: S311
            if now + wait > deadline:
                raise LLMUnavailableError(
                    "Достигнут лимит запросов к модели (LLM_RPM / LLM_TPM) — попробуйте позже"
                )
            log.info("llm_ratelimit_wait", seconds=round(wait, 1))
            self._sleep(wait)

    def settle(self, reservation: Reservation | None, actual_tokens: int) -> None:
        """Уточнить расход токенов после ответа (резерв делался по оценке)."""
        if reservation is None or actual_tokens == reservation.tokens:
            return
        if int(self._clock() // WINDOW_S) != reservation.window:
            return  # минута прошла — её счётчики уже не важны
        try:
            self._store.add(reservation.window, 0, actual_tokens - reservation.tokens)
        except RedisError:
            log.warning("llm_ratelimit_unavailable")
