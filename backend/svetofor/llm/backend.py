"""Вызов модели через OpenAI-совместимый API.

Шлюз знает только протокол `ChatBackend`; провайдер подключается переменными окружения.
Сетевые повторы (429, 5xx, обрыв связи) делает SDK: экспоненциальная пауза со случайным
разбросом и учётом `Retry-After` — свою реализацию не дублируем.

Провайдеры различаются в мелочах: одни не знают `response_format`, модели с рассуждениями
(o-серия, GPT-5) не принимают `max_tokens` и `temperature=0`. Адаптер подстраивается по тексту
ошибки 400 и запоминает это для провайдера, а не валит проверку.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import openai

from svetofor.llm.errors import (
    LLMContextTooLongError,
    LLMError,
    LLMRateLimitedError,
    LLMRequestRejectedError,
    LLMUnavailableError,
)

Role = Literal["system", "user", "assistant"]
_UNSUPPORTED = re.compile(r"unsupported|not support|only the default|does not accept")
# «This model's maximum context length is 8192 tokens…» (OpenAI, vLLM), «prompt is too long» и т. п.
_CONTEXT_TOO_LONG = re.compile(
    r"context.{0,20}length|maximum context|context window|too many tokens|prompt is too long"
    r"|max_model_len|reduce the length|input is too long"
)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    max_tokens: int
    json_schema: dict[str, Any] | None = None  # None — просим просто JSON-объект
    schema_name: str = "answer"


@dataclass(frozen=True)
class ChatReply:
    content: str
    tokens_in: int
    tokens_out: int
    finish_reason: str | None


class ChatBackend(Protocol):
    def complete(self, request: ChatRequest) -> ChatReply: ...


def _response_format(request: ChatRequest) -> dict[str, Any]:
    if request.json_schema is None:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": request.schema_name,
            "schema": request.json_schema,
            "strict": False,
        },
    }


def _map_error(exc: openai.OpenAIError) -> LLMError:
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMUnavailableError("Нет доступа к модели: проверьте ключ LLM_API_KEY")
    if isinstance(exc, openai.NotFoundError):
        return LLMUnavailableError("Модель не найдена: проверьте LLM_BASE_URL и имя модели")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("Провайдер модели ограничил частоту запросов")
    if isinstance(exc, openai.APITimeoutError):
        return LLMUnavailableError("Модель не ответила вовремя")
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailableError("Нет связи с моделью")
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        if _CONTEXT_TOO_LONG.search(str(exc).lower()):
            return LLMContextTooLongError(
                "Текст для модели длиннее её контекста: уменьшите LLM_CONTEXT_TOKENS"
            )
        return LLMRequestRejectedError("Модель отклонила запрос (возможно, слишком длинный текст)")
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return LLMUnavailableError("Модель недоступна: ошибка на стороне провайдера")
    return LLMUnavailableError("Модель недоступна")


class OpenAIBackend:
    """OpenAI-совместимый провайдер: облака с таким API, vLLM, Ollama."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float,
        supports_json_schema: bool,
        max_retries: int = 5,
        reasoning_tokens: int = 4000,
        project: str | None = None,
        client: openai.OpenAI | None = None,
    ) -> None:
        self._client = client or openai.OpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",  # локальным vLLM/Ollama ключ не нужен
            # заголовок OpenAI-Project — для провайдеров, которым нужен проект или каталог
            project=project or None,
            timeout=timeout_s,
            max_retries=max_retries,
        )
        self._supports_json_schema = supports_json_schema
        self._reasoning_tokens = reasoning_tokens  # запас на рассуждения сверх длины ответа
        # что принимает провайдер — уточняется по первым ошибкам 400 и запоминается
        self._response_format_ok = True
        self._completion_tokens = False  # max_completion_tokens вместо max_tokens
        self._temperature_ok = True

    def complete(self, request: ChatRequest) -> ChatReply:
        if not self._supports_json_schema and request.json_schema is not None:
            request = ChatRequest(
                request.model, request.messages, request.max_tokens, None, request.schema_name
            )
        reply = self._complete_adapted(request)
        if reply.finish_reason != "length" or self._completion_tokens or not self._reasoning_tokens:
            return reply
        # модели с рассуждениями, принимающие обычный max_tokens (DeepSeek через шлюз-агрегатор),
        # тратят лимит ответа на рассуждения: ответ обрывался, правила — «не проверено»
        # (21.10.2026). Один повтор с запасом; платим за него только при обрыве
        longer = replace(request, max_tokens=request.max_tokens + self._reasoning_tokens)
        second = self._complete_adapted(longer)
        return replace(
            second,
            tokens_in=reply.tokens_in + second.tokens_in,
            tokens_out=reply.tokens_out + second.tokens_out,
        )

    def _complete_adapted(self, request: ChatRequest) -> ChatReply:
        for _attempt in range(4):  # по одной подстройке на каждый параметр + сам запрос
            try:
                return self._call(request)
            except openai.BadRequestError as exc:
                if not self._adapt(str(exc)):
                    raise _map_error(exc) from exc
            except openai.OpenAIError as exc:
                raise _map_error(exc) from exc
        raise LLMRequestRejectedError("Модель отклонила запрос")

    def _adapt(self, error: str) -> bool:
        """Провайдер не принимает параметр — дальше запросы без него. False — подстроиться нечем."""
        text = error.lower()
        if self._response_format_ok and "response_format" in text:
            self._response_format_ok = False
        elif not self._completion_tokens and "max_completion_tokens" in text:
            self._completion_tokens = True
        elif self._temperature_ok and "temperature" in text and _UNSUPPORTED.search(text):
            self._temperature_ok = False
        else:
            return False
        return True

    def _call(self, request: ChatRequest) -> ChatReply:
        extra: dict[str, Any] = {}
        if self._response_format_ok:
            extra["response_format"] = _response_format(request)
        if self._completion_tokens:  # рассуждения тоже расходуют этот лимит
            extra["max_completion_tokens"] = request.max_tokens + self._reasoning_tokens
        else:
            extra["max_tokens"] = request.max_tokens
        if self._temperature_ok:
            extra["temperature"] = 0
        completion = self._client.chat.completions.create(
            model=request.model,
            messages=[{"role": m.role, "content": m.content} for m in request.messages],  # type: ignore[misc]
            **extra,
        )
        if not completion.choices:
            raise LLMUnavailableError("Модель вернула пустой ответ")
        choice = completion.choices[0]
        if choice.finish_reason == "content_filter":
            raise LLMRequestRejectedError("Модель отказалась отвечать (фильтр содержимого)")
        if getattr(choice.message, "refusal", None):
            raise LLMRequestRejectedError("Модель отказалась отвечать на запрос")
        content = choice.message.content or ""
        usage = completion.usage
        prompt_chars = sum(len(m.content) for m in request.messages)
        return ChatReply(
            content=content,
            tokens_in=usage.prompt_tokens if usage else _estimate(prompt_chars),
            tokens_out=usage.completion_tokens if usage else _estimate(len(content)),
            finish_reason=choice.finish_reason,
        )


def _estimate(chars: int) -> int:
    return max(1, round(chars / 3.5))


def schema_instructions(schema: dict[str, Any]) -> str:
    """Схема ответа текстом — для провайдеров без `json_schema`."""
    compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return f"Ответ — один JSON-объект строго по этой JSON-схеме:\n{compact}"
