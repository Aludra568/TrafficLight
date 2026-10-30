"""UUID версии 7 (RFC 9562): первые 48 бит — время в миллисекундах.

Идентификаторы сортируются по времени создания, поэтому индексы БД растут упорядоченно.
В Python 3.12 нет uuid.uuid7, поэтому — своя реализация.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Новый UUIDv7. `timestamp_ms` можно задать для тестов."""
    ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < (1 << 48):
        raise ValueError("timestamp_ms вне диапазона 48 бит")
    rand = int.from_bytes(os.urandom(10), "big")  # 80 случайных бит
    rand_a = rand >> 68 & 0xFFF  # 12 бит
    rand_b = rand & ((1 << 62) - 1)  # 62 бита
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Время создания, зашитое в UUIDv7 (миллисекунды Unix)."""
    return value.int >> 80
