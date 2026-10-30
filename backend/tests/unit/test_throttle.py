"""Ограничение попыток входа: окно, раздельные счётчики, память не растёт от перебора почт."""

from __future__ import annotations

from svetofor.api import throttle as throttle_module
from svetofor.api.throttle import LoginThrottle


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_email_limit_and_window() -> None:
    clock = Clock()
    throttle = LoginThrottle(email_limit=3, client_limit=100, window_s=60, clock=clock)
    for _ in range(3):
        assert throttle.wait_seconds("a@x.ru", "1.1.1.1") == 0
        throttle.failed("a@x.ru", "1.1.1.1")
    assert throttle.wait_seconds("a@x.ru", "2.2.2.2") == 60  # подбор к почте — с любого адреса
    assert throttle.wait_seconds("b@x.ru", "1.1.1.1") == 0  # другие учётки — можно
    clock.now += 61
    assert throttle.wait_seconds("a@x.ru", "1.1.1.1") == 0


def test_client_limit_does_not_block_other_clients() -> None:
    throttle = LoginThrottle(email_limit=100, client_limit=2, window_s=60, clock=Clock())
    throttle.failed("a@x.ru", "1.1.1.1")
    throttle.failed("b@x.ru", "1.1.1.1")
    assert throttle.wait_seconds("c@x.ru", "1.1.1.1") > 0
    assert (
        throttle.wait_seconds("c@x.ru", "2.2.2.2") == 0
    )  # поэтому адрес клиента должен быть настоящим


def test_success_resets_email_counter() -> None:
    throttle = LoginThrottle(email_limit=2, client_limit=100, window_s=60, clock=Clock())
    throttle.failed("a@x.ru", "1.1.1.1")
    throttle.succeeded("a@x.ru")
    throttle.failed("a@x.ru", "1.1.1.1")
    assert throttle.wait_seconds("a@x.ru", "1.1.1.1") == 0


def test_reads_do_not_create_keys() -> None:
    throttle = LoginThrottle(clock=Clock())
    for i in range(1000):
        throttle.wait_seconds(f"user{i}@x.ru", "1.1.1.1")
    assert throttle._failures == {}


def test_random_emails_do_not_grow_memory_forever(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(throttle_module, "MAX_KEYS", 50)
    clock = Clock()
    throttle = LoginThrottle(email_limit=10, client_limit=10**9, window_s=60, clock=clock)
    for i in range(500):
        clock.now += 0.01
        throttle.failed(f"random{i}@x.ru", "6.6.6.6")
    assert len(throttle._failures) <= 51
    assert throttle.wait_seconds("x@x.ru", "6.6.6.6") == 0
