"""Адаптер OpenAI-совместимого API вместе с настоящим SDK — на локальном сервере, без сети."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from svetofor.llm.backend import ChatRequest, Message, OpenAIBackend
from svetofor.llm.errors import LLMRequestRejectedError, LLMUnavailableError
from tests.support.fake_llm import FakeLLMServer

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
REQUEST = ChatRequest(
    model="test-model",
    messages=(Message("system", "Отвечай JSON."), Message("user", "Есть ли риск?")),
    max_tokens=100,
    json_schema=SCHEMA,
)
FAST_RETRY = {"retry-after-ms": "5"}  # SDK ждёт 5 мс вместо экспоненциальной паузы


@pytest.fixture
def server() -> Iterator[FakeLLMServer]:
    with FakeLLMServer() as srv:
        yield srv


def backend(server: FakeLLMServer, *, schema: bool = True, retries: int = 2) -> OpenAIBackend:
    return OpenAIBackend(
        base_url=server.url,
        api_key="",
        timeout_s=5,
        supports_json_schema=schema,
        max_retries=retries,
    )


def test_success_and_request_shape(server: FakeLLMServer) -> None:
    server.reply('{"ok": true}')
    reply = backend(server).complete(REQUEST)
    assert reply.content == '{"ok": true}'
    assert (reply.tokens_in, reply.tokens_out, reply.finish_reason) == (11, 7, "stop")
    sent = server.requests[0]
    assert sent["model"] == "test-model" and sent["temperature"] == 0 and sent["max_tokens"] == 100
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA


def test_project_header_for_folder_providers(server: FakeLLMServer) -> None:
    """Проект или каталог провайдера уходит в заголовке OpenAI-Project — `LLM_PROJECT`."""
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')
    OpenAIBackend(
        base_url=server.url,
        api_key="k",
        timeout_s=5,
        supports_json_schema=True,
        project="b1gfolder",
    ).complete(REQUEST)
    backend(server).complete(REQUEST)
    assert server.headers[0]["openai-project"] == "b1gfolder"
    assert "openai-project" not in server.headers[1]  # без настройки — как раньше


def test_json_object_when_schema_not_supported(server: FakeLLMServer) -> None:
    server.reply('{"ok": true}')
    backend(server, schema=False).complete(REQUEST)
    assert server.requests[0]["response_format"] == {"type": "json_object"}


def test_retries_rate_limit_then_succeeds(server: FakeLLMServer) -> None:
    server.error(429, "slow down", FAST_RETRY)
    server.error(503, "busy", FAST_RETRY)
    server.reply('{"ok": false}')
    assert backend(server, retries=3).complete(REQUEST).content == '{"ok": false}'
    assert len(server.requests) == 3


def test_server_errors_exhaust_retries(server: FakeLLMServer) -> None:
    for _ in range(3):
        server.error(500, "boom", FAST_RETRY)
    with pytest.raises(LLMUnavailableError, match="ошибка на стороне провайдера"):
        backend(server, retries=2).complete(REQUEST)
    assert len(server.requests) == 3


def test_bad_key(server: FakeLLMServer) -> None:
    server.error(401, "invalid api key")
    with pytest.raises(LLMUnavailableError, match="LLM_API_KEY"):
        backend(server).complete(REQUEST)
    assert len(server.requests) == 1  # 401 не повторяем


def test_provider_without_response_format(server: FakeLLMServer) -> None:
    server.error(400, "Unknown parameter: response_format")
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')
    client = backend(server)
    assert client.complete(REQUEST).content == '{"ok": true}'
    assert "response_format" in server.requests[0]
    assert "response_format" not in server.requests[1]
    client.complete(REQUEST)  # запомнил: больше не отправляет
    assert "response_format" not in server.requests[2]


def test_truncated_reply_retried_once_with_reasoning_budget(server: FakeLLMServer) -> None:
    """Модель с рассуждениями (DeepSeek через max_tokens) тратит на них лимит ответа: ответ
    обрывается, правило — «не проверено» (21.10.2026). Один повтор с запасом рассуждений."""
    server.reply('{"ok": tr', finish_reason="length")
    server.reply('{"ok": true}')
    reply = backend(server).complete(REQUEST)
    assert reply.content == '{"ok": true}' and reply.finish_reason == "stop"
    assert (reply.tokens_in, reply.tokens_out) == (22, 14)  # оплачены оба ответа
    assert [r["max_tokens"] for r in server.requests] == [100, 100 + 4000]


def test_truncated_twice_is_reported(server: FakeLLMServer) -> None:
    server.reply('{"ok": tr', finish_reason="length")
    server.reply('{"ok": tr', finish_reason="length")
    assert backend(server).complete(REQUEST).finish_reason == "length"
    assert len(server.requests) == 2  # не больше одного повтора


def test_reasoning_model_parameters_adapted(server: FakeLLMServer) -> None:
    """o-серия / GPT-5: max_completion_tokens вместо max_tokens и без temperature=0."""
    server.error(
        400,
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
    )
    server.error(
        400,
        "Unsupported value: 'temperature' does not support 0 with this model. "
        "Only the default (1) value is supported.",
    )
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')
    client = backend(server)
    assert client.complete(REQUEST).content == '{"ok": true}'
    final = server.requests[2]
    assert "max_tokens" not in final and "temperature" not in final
    assert final["max_completion_tokens"] == 100 + 4000  # запас на рассуждения
    client.complete(REQUEST)  # запомнил: второй запрос — сразу в нужном виде
    assert len(server.requests) == 4 and "max_tokens" not in server.requests[3]


def test_context_length_error_is_not_adapted(server: FakeLLMServer) -> None:
    server.error(400, "max_tokens is too large: 9000. This model supports at most 4096 tokens")
    with pytest.raises(LLMRequestRejectedError):
        backend(server).complete(REQUEST)
    assert len(server.requests) == 1


def test_context_too_long_is_its_own_error(server: FakeLLMServer) -> None:
    from svetofor.llm.errors import LLMContextTooLongError

    server.error(
        400,
        "This model's maximum context length is 8192 tokens. However, you requested 9500 tokens.",
    )
    with pytest.raises(LLMContextTooLongError, match="LLM_CONTEXT_TOKENS"):
        backend(server).complete(REQUEST)


def test_refusal_is_honest(server: FakeLLMServer) -> None:
    server.reply("", refusal="I can't help with that.")
    with pytest.raises(LLMRequestRejectedError, match="отказалась"):
        backend(server).complete(REQUEST)


def test_other_bad_request(server: FakeLLMServer) -> None:
    server.error(400, "context length exceeded")
    with pytest.raises(LLMRequestRejectedError):
        backend(server).complete(REQUEST)


def test_content_filter(server: FakeLLMServer) -> None:
    server.reply("", finish_reason="content_filter")
    with pytest.raises(LLMRequestRejectedError, match="фильтр"):
        backend(server).complete(REQUEST)


def test_usage_missing_is_estimated(server: FakeLLMServer) -> None:
    server.reply('{"ok": true}', usage=False)
    reply = backend(server).complete(REQUEST)
    assert reply.tokens_in > 0 and reply.tokens_out > 0


def test_unreachable_provider() -> None:
    client = OpenAIBackend(
        base_url="http://127.0.0.1:9/v1",
        api_key="",
        timeout_s=2,
        supports_json_schema=False,
        max_retries=0,
    )
    # Linux сразу отказывает в соединении, Windows ждёт до тайм-аута — оба случая «недоступна»
    with pytest.raises(LLMUnavailableError, match=r"Нет связи|не ответила вовремя"):
        client.complete(REQUEST)
