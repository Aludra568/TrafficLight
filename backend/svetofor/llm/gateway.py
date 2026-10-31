"""Шлюз LLM — единственная точка вызова моделей.

Кэш → (replay: только кэш) → ограничитель частоты → вызов → разбор JSON → проверка схемой →
одна попытка исправления с текстом ошибки → кэш, журнал, метрики. Любая неудача — исключение
`LLMError` с сообщением по-русски: вызывающий код помечает правила «не проверено».
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, NoReturn, Protocol

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from svetofor.core.config import LLMMode, Settings
from svetofor.core.logging import get_logger
from svetofor.core.metrics import LLM_CACHE_HITS, LLM_ERRORS, LLM_LATENCY, LLM_TOKENS
from svetofor.db.models import LLMCall
from svetofor.llm.backend import (
    ChatBackend,
    ChatReply,
    ChatRequest,
    Message,
    OpenAIBackend,
    schema_instructions,
)
from svetofor.llm.cache import CachedReply, LLMCache, cache_key
from svetofor.llm.errors import (
    LLMError,
    LLMRateLimitedError,
    LLMReplayMissError,
    LLMSchemaError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from svetofor.llm.ratelimit import RateLimiter

log = get_logger(__name__)
ModelRole = Literal["main", "fast"]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_MAX_REPAIR_ERRORS = 8
TRUNCATED_MESSAGE = "Ответ модели оборвался: не хватило лимита длины ответа"
# 429 от провайдера: SDK повторяет за ~15 с, а лимиты провайдеров — поминутные. Шлюз ждёт ещё
# 15, 30 и 45 с (не больше 1,5 минуты на вызов: задачи проверки ограничены по времени)
RATE_LIMIT_WAITS = (15.0, 30.0, 45.0)


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    value: T
    model: str
    prompt_version: str
    cache_hit: bool
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class CallRecord:
    task: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    cache_hit: bool
    error: str | None
    check_id: uuid.UUID | None


class CallLog(Protocol):
    def record(self, call: CallRecord) -> None: ...


class DbCallLog:
    """Журнал `llm_calls`. Сбой записи журнала не должен ронять проверку."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(self, call: CallRecord) -> None:
        try:
            with self._session_factory() as session:
                session.add(
                    LLMCall(
                        check_id=call.check_id,
                        task=call.task,
                        model=call.model,
                        latency_ms=call.latency_ms,
                        tokens_in=call.tokens_in,
                        tokens_out=call.tokens_out,
                        cache_hit=call.cache_hit,
                        error=call.error,
                    )
                )
                session.commit()
        except Exception:  # журнал вспомогательный: ошибка БД здесь не важнее ответа модели
            log.warning("llm_call_log_failed", task=call.task, exc_info=True)


def extract_json(text: str) -> str:
    """JSON-объект из ответа: без рассуждений <think>, обёртки ```json и пояснений вокруг."""
    stripped = _THINK.sub("", text).strip()
    fence = _FENCE.match(stripped)
    if fence:
        stripped = fence.group(1).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    return stripped[start : end + 1]


def parse_reply[T: BaseModel](content: str, schema: type[T]) -> T:
    raw = json.loads(extract_json(content), parse_constant=_reject_non_finite)
    return schema.model_validate(_storable(raw))


def _reject_non_finite(name: str) -> float:
    """NaN и Infinity — не JSON: PostgreSQL отвергает их в jsonb, и вся пачка правил стала бы
    «не проверено». Ошибка здесь уходит в обычную попытку исправления ответа."""
    raise ValueError(f"в ответе нечисловое значение {name}")


# NUL и одиночные суррогаты (\u0000, \ud800 в JSON) PostgreSQL не примет: запись замечания
# упала бы, и вся пачка правил стала бы «не проверено» из-за одного символа в ответе модели
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


def _storable(value: Any) -> Any:
    if isinstance(value, str):
        return _UNSTORABLE.sub("", value)
    if isinstance(value, list):
        return [_storable(item) for item in value]
    if isinstance(value, dict):
        return {_storable(key): _storable(item) for key, item in value.items()}
    return value


def repair_prompt(error: Exception) -> str:
    if isinstance(error, ValidationError):
        problems = [
            f"- {'.'.join(str(p) for p in item['loc']) or 'ответ'}: {item['msg']}"
            for item in error.errors()[:_MAX_REPAIR_ERRORS]
        ]
        details = "\n".join(problems)
    else:
        details = f"- {error}"
    return (
        "Ответ не прошёл проверку:\n"
        f"{details}\n"
        "Верни исправленный ответ — только JSON-объект по схеме, без пояснений."
    )


def _estimate_tokens(messages: tuple[Message, ...], max_tokens: int) -> int:
    return round(sum(len(m.content) for m in messages) / 3.5) + max_tokens


class LLMGateway:
    def __init__(
        self,
        *,
        mode: LLMMode,
        model_main: str,
        model_fast: str,
        backend: ChatBackend | None,
        cache: LLMCache,
        limiter: RateLimiter | None = None,
        call_log: CallLog | None = None,
        supports_json_schema: bool = False,
        log_bodies: bool = False,
        clock: Callable[[], float] = time.perf_counter,
        cassette: LLMCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mode = mode
        # кассета демо: ответы настоящей модели под ключом без имени модели — так их находит
        # режим replay, где модели нет («Попробовать на примере» без ключа)
        self._cassette = cassette
        self._models: dict[ModelRole, str] = {"main": model_main, "fast": model_fast or model_main}
        self._backend = backend
        self._cache = cache
        self._limiter = limiter
        self._call_log = call_log
        self._supports_json_schema = supports_json_schema
        self._log_bodies = log_bodies
        self._clock = clock
        self._sleep = sleep

    def complete_json[T: BaseModel](
        self,
        *,
        task: str,
        model_role: ModelRole,
        system: str,
        user: str,
        schema: type[T],
        prompt_version: str,
        max_tokens: int = 2000,
        check_id: uuid.UUID | None = None,
        thinking: bool = True,
    ) -> LLMResult[T]:
        if not thinking:
            prompt_version = f"{prompt_version}+nothink"
        model = self._models[model_role]
        messages = (Message("system", system), Message("user", user))
        key = cache_key(model, messages, prompt_version, schema)
        if self.mode != LLMMode.RECORD:
            hit = self._from_cache(key, schema)
            if hit is not None:
                cached, value = hit
                self._to_cassette(task, messages, prompt_version, schema, cached)
                LLM_CACHE_HITS.labels(task).inc()
                self._record(
                    task, model, 0, cached.tokens_in, cached.tokens_out, True, None, check_id
                )
                return LLMResult(
                    value, model, prompt_version, True, cached.tokens_in, cached.tokens_out
                )
        if self.mode == LLMMode.REPLAY:
            self._fail(
                task,
                model,
                LLMReplayMissError(
                    "Нет записанного ответа модели для этого запроса: "
                    "модель не подключена (демо-режим)"
                ),
                check_id,
            )
        if self._backend is None:
            self._fail(
                task,
                model,
                LLMUnavailableError("Модель не подключена: задайте LLM_BASE_URL и LLM_MODEL_MAIN"),
                check_id,
            )

        started = self._clock()
        try:
            value, tokens_in, tokens_out = self._ask(
                model, messages, schema, max_tokens, thinking=thinking
            )
        except LLMError as exc:
            self._fail(task, model, exc, check_id, started)
        latency = self._clock() - started
        response = value.model_dump(mode="json")
        reply = CachedReply(task, model, prompt_version, response, tokens_in, tokens_out)
        self._cache.put(key, reply)
        self._to_cassette(task, messages, prompt_version, schema, reply)
        LLM_LATENCY.labels(task, model).observe(latency)
        LLM_TOKENS.labels("in", model).inc(tokens_in)
        LLM_TOKENS.labels("out", model).inc(tokens_out)
        self._record(task, model, latency, tokens_in, tokens_out, False, None, check_id)
        if self._log_bodies:
            log.debug("llm_call_body", task=task, system=system, user=user, response=response)
        return LLMResult(value, model, prompt_version, False, tokens_in, tokens_out)

    def _to_cassette(
        self,
        task: str,
        messages: tuple[Message, ...],
        prompt_version: str,
        schema: type[BaseModel],
        reply: CachedReply,
    ) -> None:
        if self._cassette is None or self.mode == LLMMode.REPLAY:
            return
        key = cache_key("", messages, prompt_version, schema)
        self._cassette.put(key, CachedReply(task, "", prompt_version, reply.response))

    def _from_cache[T: BaseModel](self, key: str, schema: type[T]) -> tuple[CachedReply, T] | None:
        cached = self._cache.get(key)
        if cached is None:
            return None
        try:
            return cached, schema.model_validate(cached.response)
        except ValidationError:
            log.warning("llm_cache_entry_invalid", key=key)  # схема ужесточилась — переспросим
            return None

    def _send(self, request: ChatRequest) -> ChatReply:
        assert self._backend is not None  # проверено в complete_json
        for wait in (*RATE_LIMIT_WAITS, None):
            reservation = (
                self._limiter.acquire(_estimate_tokens(request.messages, request.max_tokens))
                if self._limiter
                else None
            )
            try:
                reply = self._backend.complete(request)
            except LLMRateLimitedError:
                if wait is None:
                    raise
                log.info("llm_rate_limited_wait", seconds=wait)
                self._sleep(wait)
                continue
            if self._limiter:
                self._limiter.settle(reservation, reply.tokens_in + reply.tokens_out)
            return reply
        raise AssertionError("недостижимо: последний проход либо отвечает, либо бросает")

    def _ask[T: BaseModel](
        self,
        model: str,
        messages: tuple[Message, ...],
        schema: type[T],
        max_tokens: int,
        thinking: bool = True,
    ) -> tuple[T, int, int]:
        json_schema = schema.model_json_schema()
        system = Message("system", f"{messages[0].content}\n\n{schema_instructions(json_schema)}")
        request = ChatRequest(
            model=model,
            messages=(system, *messages[1:]),
            max_tokens=max_tokens,
            json_schema=json_schema if self._supports_json_schema else None,
            schema_name=schema.__name__,
            thinking=thinking,
        )
        reply = self._send(request)
        tokens_in, tokens_out = reply.tokens_in, reply.tokens_out
        try:
            return parse_reply(reply.content, schema), tokens_in, tokens_out
        except (ValidationError, ValueError) as exc:
            first_error: Exception = exc
        if reply.finish_reason == "length":
            # ответ оборвался: «исправить» с тем же лимитом бесполезно — один раз с вдвое большим
            request = replace(request, max_tokens=max_tokens * 2)
            reply = self._send(request)
            tokens_in, tokens_out = tokens_in + reply.tokens_in, tokens_out + reply.tokens_out
            try:
                return parse_reply(reply.content, schema), tokens_in, tokens_out
            except (ValidationError, ValueError) as exc:
                if reply.finish_reason == "length":
                    raise LLMTruncatedError(TRUNCATED_MESSAGE) from exc
                first_error = exc
        repair = ChatRequest(
            model=model,
            messages=(
                *request.messages,
                Message("assistant", reply.content),
                Message("user", repair_prompt(first_error)),
            ),
            max_tokens=request.max_tokens,
            json_schema=request.json_schema,
            schema_name=request.schema_name,
            thinking=thinking,
        )
        second = self._send(repair)
        tokens_in, tokens_out = tokens_in + second.tokens_in, tokens_out + second.tokens_out
        try:
            return parse_reply(second.content, schema), tokens_in, tokens_out
        except (ValidationError, ValueError) as exc:
            if second.finish_reason == "length":
                raise LLMTruncatedError(TRUNCATED_MESSAGE) from exc
            raise LLMSchemaError("Ответ модели не прошёл проверку формата") from exc

    def _record(
        self,
        task: str,
        model: str,
        latency_s: float,
        tokens_in: int,
        tokens_out: int,
        cache_hit: bool,
        error: str | None,
        check_id: uuid.UUID | None,
    ) -> None:
        if self._call_log is None:
            return
        self._call_log.record(
            CallRecord(
                task,
                model,
                round(latency_s * 1000),
                tokens_in,
                tokens_out,
                cache_hit,
                error,
                check_id,
            )
        )

    def _fail(
        self,
        task: str,
        model: str,
        error: LLMError,
        check_id: uuid.UUID | None,
        started: float | None = None,
    ) -> NoReturn:
        LLM_ERRORS.labels(task, error.kind).inc()
        latency = self._clock() - started if started is not None else 0.0
        self._record(task, model, latency, 0, 0, False, f"{error.kind}: {error.message}", check_id)
        log.warning("llm_call_failed", task=task, model=model, kind=error.kind)
        raise error


def build_gateway(
    settings: Settings,
    *,
    cache: LLMCache,
    limiter: RateLimiter | None = None,
    call_log: CallLog | None = None,
    cassette: LLMCache | None = None,
) -> LLMGateway:
    """Шлюз по настройкам окружения. Без провайдера — режим replay (демо без ключа)."""
    backend: ChatBackend | None = None
    if settings.llm_configured:
        backend = OpenAIBackend(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_s=settings.llm_timeout_s,
            supports_json_schema=settings.llm_supports_json_schema,
            reasoning_tokens=settings.llm_reasoning_tokens,
            project=settings.llm_project,
            thinking_toggle=settings.llm_thinking_toggle,
        )
    return LLMGateway(
        mode=settings.effective_llm_mode,
        model_main=settings.llm_model_main,
        model_fast=settings.llm_model_fast,
        backend=backend,
        cache=cache,
        limiter=limiter,
        call_log=call_log,
        supports_json_schema=settings.llm_supports_json_schema,
        log_bodies=settings.llm_log_bodies,
        cassette=cassette,
    )
