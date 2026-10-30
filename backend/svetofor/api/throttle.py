"""Ограничение попыток входа.

Неудачные попытки считаются по адресу почты (подбор пароля к одной учётной записи) и по адресу
клиента (перебор многих учётных записей). Окно скользящее; успешный вход сбрасывает счёт почты.
Счётчики живут в памяти процесса API: при нескольких процессах лимит действует в каждом —
достаточно, чтобы перебор стал бессмысленно медленным, и не требует Redis для входа.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

EMAIL_LIMIT = 10  # неудачных попыток на почту за окно
CLIENT_LIMIT = 50  # неудачных попыток с одного адреса за окно
WINDOW_S = 15 * 60
MAX_KEYS = 100_000  # перебор случайных адресов почты не должен раздувать память процесса


class LoginThrottle:
    def __init__(
        self,
        email_limit: int = EMAIL_LIMIT,
        client_limit: int = CLIENT_LIMIT,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = {"email": email_limit, "client": client_limit}
        self._window = window_s
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        times = self._failures.get(key) or deque()
        while times and now - times[0] > self._window:
            times.popleft()
        return times

    def _prune(self, now: float) -> None:
        """Забыть ключи, у которых все неудачи старше окна; при переполнении — самые старые."""
        stale = [k for k, t in self._failures.items() if not t or now - t[-1] > self._window]
        for key in stale:
            del self._failures[key]
        if len(self._failures) >= MAX_KEYS:
            oldest = sorted(self._failures, key=lambda k: self._failures[k][-1])
            for key in oldest[: len(oldest) // 2]:
                del self._failures[key]

    def wait_seconds(self, email: str, client: str) -> int:
        """Сколько ждать до следующей попытки; 0 — можно пробовать."""
        now = self._clock()
        with self._lock:
            wait = 0.0
            for kind, value in (("email", email), ("client", client)):
                times = self._recent((kind, value), now)
                if len(times) >= self._limits[kind]:
                    wait = max(wait, self._window - (now - times[0]))
            return math.ceil(wait)

    def failed(self, email: str, client: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._failures) >= MAX_KEYS:
                self._prune(now)
            for key in (("email", email), ("client", client)):
                times = self._recent(key, now)
                times.append(now)
                self._failures[key] = times

    def succeeded(self, email: str) -> None:
        with self._lock:
            self._failures.pop(("email", email), None)
