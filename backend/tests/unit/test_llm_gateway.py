"""Шлюз LLM: режимы live/record/replay, кэш, разбор и исправление ответа, журнал, лимиты."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

import pytest
from prometheus_client import REGISTRY
from pydantic import BaseModel, Field

from svetofor.core.config import LLMMode, Settings
from svetofor.llm.backend import ChatRequest
from svetofor.llm.cache import (
    CachedReply,
    JsonlCache,
    LayeredCache,
    MemoryCache,
    ReadOnlyCache,
    cache_key,
)
from svetofor.llm.errors import (
    LLMRateLimitedError,
    LLMReplayMissError,
    LLMSchemaError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from svetofor.llm.gateway import (
    CallRecord,
    LLMGateway,
    build_gateway,
    extract_json,
    parse_reply,
    repair_prompt,
)
from tests.support.fake_llm import ScriptedBackend, Truncated


class Verdict(BaseModel):
    rule_id: str
    verdict: Literal["violation", "ok", "not_found"]
    quote: str | None = Field(None, max_length=600)


GOOD = '{"rule_id": "PAY-01", "verdict": "violation", "quote": "оплата в течение 30 дней"}'
CALL: dict[str, Any] = {
    "task": "check_group",
    "model_role": "main",
    "system": "Системный промпт",
    "user": "<<<ДОКУМЕНТ\nтекст\nДОКУМЕНТ>>>",
    "schema": Verdict,
    "prompt_version": "check_group.v1",
}


class ListLog:
    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    def record(self, call: CallRecord) -> None:
        self.calls.append(call)


def gateway(
    backend: ScriptedBackend | None,
    mode: LLMMode = LLMMode.LIVE,
    cache: MemoryCache | None = None,
    **kw: Any,
) -> LLMGateway:
    return LLMGateway(
        mode=mode,
        model_main="main-model",
        model_fast="fast-model",
        backend=backend,
        cache=cache if cache is not None else MemoryCache(),
        **kw,
    )


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ─── Режимы и кэш ───


def test_live_asks_once_then_uses_cache() -> None:
    backend, log = ScriptedBackend(GOOD), ListLog()
    gw = gateway(backend, call_log=log)
    first = gw.complete_json(**CALL)
    second = gw.complete_json(**CALL)
    assert first.value == second.value and first.value.verdict == "violation"
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert len(backend.requests) == 1
    assert [c.cache_hit for c in log.calls] == [False, True]
    assert first.model == "main-model" and first.prompt_version == "check_group.v1"


def test_fast_role_uses_fast_model() -> None:
    backend = ScriptedBackend(GOOD)
    assert gateway(backend).complete_json(**{**CALL, "model_role": "fast"}).model == "fast-model"
    assert backend.requests[0].model == "fast-model"


def test_fast_role_falls_back_to_main() -> None:
    gw = LLMGateway(
        mode=LLMMode.LIVE,
        model_main="main-model",
        model_fast="",
        backend=ScriptedBackend(GOOD),
        cache=MemoryCache(),
    )
    assert gw.complete_json(**{**CALL, "model_role": "fast"}).model == "main-model"


def test_record_mode_always_asks_and_overwrites() -> None:
    cache = MemoryCache()
    gateway(ScriptedBackend(GOOD), cache=cache).complete_json(**CALL)
    newer = GOOD.replace("violation", "ok")
    backend = ScriptedBackend(newer)
    result = gateway(backend, LLMMode.RECORD, cache).complete_json(**CALL)
    assert result.value.verdict == "ok" and not result.cache_hit and len(backend.requests) == 1
    assert gateway(None, LLMMode.REPLAY, cache).complete_json(**CALL).value.verdict == "ok"


def test_replay_hit_and_miss() -> None:
    cache = MemoryCache()
    gateway(ScriptedBackend(GOOD), cache=cache).complete_json(**CALL)
    replay = gateway(None, LLMMode.REPLAY, cache)
    assert replay.complete_json(**CALL).cache_hit
    before = metric("svetofor_llm_errors_total", task="check_group", kind="replay_miss")
    with pytest.raises(LLMReplayMissError, match="демо-режим"):
        replay.complete_json(**{**CALL, "user": "другой документ"})
    assert metric("svetofor_llm_errors_total", task="check_group", kind="replay_miss") == before + 1


def test_live_without_backend() -> None:
    with pytest.raises(LLMUnavailableError, match="LLM_BASE_URL"):
        gateway(None).complete_json(**CALL)


def test_cache_key_depends_on_everything_that_changes_the_answer() -> None:
    from svetofor.llm.backend import Message

    class Other(BaseModel):
        answer: str

    messages = (Message("system", "s"), Message("user", "u"))
    base = cache_key("m", messages, "p.v1", Verdict)
    assert base == cache_key("m", messages, "p.v1", Verdict)
    assert base != cache_key("m2", messages, "p.v1", Verdict)
    assert base != cache_key("m", messages, "p.v2", Verdict)
    assert base != cache_key("m", messages, "p.v1", Other)
    assert base != cache_key("m", (Message("system", "s"), Message("user", "u2")), "p.v1", Verdict)


def test_stale_cache_entry_is_reasked() -> None:
    cache = MemoryCache()
    from svetofor.llm.backend import Message

    key = cache_key(
        "main-model",
        (Message("system", CALL["system"]), Message("user", CALL["user"])),
        CALL["prompt_version"],
        Verdict,
    )
    cache.put(key, CachedReply("check_group", "main-model", "check_group.v1", {"wrong": 1}))
    backend = ScriptedBackend(GOOD)
    result = gateway(backend, cache=cache).complete_json(**CALL)
    assert not result.cache_hit and len(backend.requests) == 1


# ─── Разбор и исправление ответа ───


@pytest.mark.parametrize(
    "content",
    [
        GOOD,
        f"```json\n{GOOD}\n```",
        f"Вот ответ:\n{GOOD}\nНадеюсь, помог.",
        f"<think>Рассуждаю {{о скобках}}…</think>\n{GOOD}",
    ],
)
def test_extract_json_variants(content: str) -> None:
    assert Verdict.model_validate_json(extract_json(content)).rule_id == "PAY-01"


def test_reply_without_characters_postgres_rejects() -> None:
    """NUL и одиночный суррогат в ответе модели не должны уронить запись замечания."""
    content = '{"rule_id": "PAY-01\\u0000", "verdict": "ok", "quote": "оплата\\ud800 в срок"}'
    value = parse_reply(content, Verdict)
    assert value.rule_id == "PAY-01" and value.quote == "оплата в срок"
    value.quote.encode("utf-8")  # без суррогатов строка кодируется — PostgreSQL её примет


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_a_schema_error(constant: str) -> None:
    """Код-ревью 2026-10-08: NaN из ответа модели доходил до jsonb и ронял запись пачки правил."""

    class Scored(BaseModel):
        score: float

    with pytest.raises(ValueError, match="нечисловое"):
        parse_reply(f'{{"score": {constant}}}', Scored)


def test_extract_json_without_object() -> None:
    with pytest.raises(ValueError, match="нет JSON"):
        extract_json("Рисков нет.")


def test_schema_and_instructions_in_request() -> None:
    backend = ScriptedBackend(GOOD)
    gateway(backend, supports_json_schema=True).complete_json(**CALL)
    request: ChatRequest = backend.requests[0]
    assert request.json_schema == Verdict.model_json_schema()
    assert request.messages[0].content.startswith("Системный промпт")
    assert "JSON-схеме" in request.messages[0].content
    assert request.messages[1].content == CALL["user"]
    backend = ScriptedBackend(GOOD)
    gateway(backend, supports_json_schema=False).complete_json(**CALL)
    assert backend.requests[0].json_schema is None


def test_repair_after_invalid_answer() -> None:
    bad = '{"rule_id": "PAY-01", "verdict": "maybe"}'
    backend = ScriptedBackend(bad, GOOD)
    result = gateway(backend).complete_json(**CALL)
    assert result.value.verdict == "violation"
    assert (result.tokens_in, result.tokens_out) == (200, 40)  # обе попытки учтены
    repair = backend.requests[1].messages
    assert [m.role for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2].content == bad
    assert "verdict" in repair[3].content and "только JSON-объект" in repair[3].content


def test_two_invalid_answers_fail_honestly() -> None:
    backend = ScriptedBackend("Не знаю", '{"rule_id": 5}')
    log = ListLog()
    with pytest.raises(LLMSchemaError, match="не прошёл проверку формата"):
        gateway(backend, call_log=log).complete_json(**CALL)
    assert log.calls[-1].error is not None and log.calls[-1].error.startswith("schema")


def test_truncated_answer_explained_without_useless_repair() -> None:
    """Оборванный ответ переспрашивается один раз с вдвое большим лимитом, а не «исправляется»
    с тем же: тот снова оборвётся. Оборвался и второй — честная ошибка «оборвался»."""
    backend = ScriptedBackend('{"rule_id": "PAY', '{"rule_id": "PA', finish_reason="length")
    with pytest.raises(LLMTruncatedError, match="оборвался") as raised:
        gateway(backend).complete_json(**CALL, max_tokens=1000)
    assert isinstance(raised.value, LLMSchemaError) and raised.value.kind == "truncated"
    assert [r.max_tokens for r in backend.requests] == [1000, 2000]


def test_truncated_answer_succeeds_with_larger_limit() -> None:
    backend = ScriptedBackend(Truncated('{"rule_id": "PAY'), GOOD)
    result = gateway(backend).complete_json(**CALL, max_tokens=1000)
    assert result.value.verdict == "violation"
    assert [r.max_tokens for r in backend.requests] == [1000, 2000]


def test_rate_limit_waits_and_retries() -> None:
    """429 провайдера: шлюз ждёт 15 и 30 с и повторяет — проверка не теряет правило из-за
    поминутного лимита, который короткие повторы SDK не переживают."""
    waits: list[float] = []
    limited = LLMRateLimitedError("Провайдер модели ограничил частоту запросов")
    backend = ScriptedBackend(limited, limited, GOOD)
    result = gateway(backend, sleep=waits.append).complete_json(**CALL)
    assert result.value.verdict == "violation" and waits == [15.0, 30.0]


def test_rate_limit_gives_up_after_waits() -> None:
    waits: list[float] = []
    limited = LLMRateLimitedError("Провайдер модели ограничил частоту запросов")
    log = ListLog()
    backend = ScriptedBackend(limited, limited, limited, limited)
    with pytest.raises(LLMUnavailableError, match="ограничил частоту"):
        gateway(backend, sleep=waits.append, call_log=log).complete_json(**CALL)
    assert waits == [15.0, 30.0, 45.0] and len(backend.requests) == 4
    assert log.calls[-1].error is not None and log.calls[-1].error.startswith("rate_limited")


def test_other_failures_are_not_waited_for() -> None:
    waits: list[float] = []
    backend = ScriptedBackend(LLMUnavailableError("Нет связи с моделью"))
    with pytest.raises(LLMUnavailableError):
        gateway(backend, sleep=waits.append).complete_json(**CALL)
    assert waits == []


def test_failed_answer_is_not_cached() -> None:
    cache = MemoryCache()
    with pytest.raises(LLMSchemaError):
        gateway(ScriptedBackend("нет", "нет"), cache=cache).complete_json(**CALL)
    assert len(cache) == 0


def test_backend_error_propagates_and_is_logged() -> None:
    log = ListLog()
    backend = ScriptedBackend(LLMUnavailableError("Нет связи с моделью"))
    check_id = uuid.uuid4()
    with pytest.raises(LLMUnavailableError):
        gateway(backend, call_log=log).complete_json(**CALL, check_id=check_id)
    assert log.calls[0].error == "unavailable: Нет связи с моделью"
    assert log.calls[0].check_id == check_id


def test_repair_prompt_lists_fields() -> None:
    try:
        Verdict.model_validate({"rule_id": "X", "verdict": "bad"})
    except Exception as exc:
        text = repair_prompt(exc)
    assert "- verdict:" in text


# ─── Ограничитель и сборка по настройкам ───


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired: list[int] = []
        self.settled: list[int] = []

    def acquire(self, tokens: int) -> str:
        self.acquired.append(tokens)
        return "reservation"

    def settle(self, reservation: object, actual: int) -> None:
        self.settled.append(actual)


def test_limiter_reserves_and_settles() -> None:
    limiter = CountingLimiter()
    gateway(ScriptedBackend(GOOD), limiter=limiter).complete_json(**CALL, max_tokens=500)  # type: ignore[arg-type]
    assert len(limiter.acquired) == 1 and limiter.acquired[0] > 500
    assert limiter.settled == [120]


def test_build_gateway_without_provider_is_replay() -> None:
    settings = Settings(llm_base_url="", llm_model_main="demo-model", llm_mode=LLMMode.LIVE)
    gw = build_gateway(settings, cache=MemoryCache())
    assert gw.mode == LLMMode.REPLAY
    with pytest.raises(LLMReplayMissError):
        gw.complete_json(**CALL)


def test_build_gateway_with_provider() -> None:
    settings = Settings(
        llm_base_url="http://127.0.0.1:9/v1", llm_model_main="m", llm_mode=LLMMode.RECORD
    )
    assert build_gateway(settings, cache=MemoryCache()).mode == LLMMode.RECORD


# ─── Кассета демо: ответы модели для «Попробовать на примере» без ключа ───


def replay_gateway(cassette_path: Path) -> LLMGateway:
    """Как в демо без ключа: режим replay, имени модели нет, кассета — только для чтения."""
    return LLMGateway(
        mode=LLMMode.REPLAY,
        model_main="",
        model_fast="",
        backend=None,
        cache=LayeredCache(MemoryCache(), ReadOnlyCache(JsonlCache(cassette_path))),
    )


def test_cassette_recorded_with_model_replays_without_it(tmp_path: Path) -> None:
    tape = tmp_path / "demo.jsonl"
    live = gateway(ScriptedBackend(GOOD), cassette=JsonlCache(tape))
    recorded = live.complete_json(**CALL)
    replayed = replay_gateway(tape).complete_json(**CALL)
    assert replayed.value == recorded.value and replayed.cache_hit


def test_cassette_records_answers_already_in_cache(tmp_path: Path) -> None:
    """Повторная запись кассеты, когда ответы уже в Redis/БД: они тоже попадают в кассету."""
    cache, tape = MemoryCache(), tmp_path / "demo.jsonl"
    gateway(ScriptedBackend(GOOD), cache=cache).complete_json(**CALL)
    gateway(None, cache=cache, cassette=JsonlCache(tape)).complete_json(**CALL)
    assert replay_gateway(tape).complete_json(**CALL).value.verdict == "violation"


def test_replay_never_writes_to_cassette_file(tmp_path: Path) -> None:
    tape = tmp_path / "demo.jsonl"
    gateway(ScriptedBackend(GOOD), cassette=JsonlCache(tape)).complete_json(**CALL)
    before = tape.read_text(encoding="utf-8")
    other = {**CALL, "user": "другой промпт"}
    with pytest.raises(LLMReplayMissError):
        replay_gateway(tape).complete_json(**other)  # нет ответа — честный сбой, не выдумка
    assert tape.read_text(encoding="utf-8") == before
