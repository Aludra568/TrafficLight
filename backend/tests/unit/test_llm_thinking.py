"""Тесты отключения рассуждений модели (thinking: disabled).

Проверяются:
- передача extra_body={"thinking": {"type": "disabled"}} при thinking=False;
- отсутствие extra_body при thinking=True и при thinking_toggle=False;
- обработка BadRequestError про thinking (подстройка и повтор);
- проброс других BadRequestError;
- различие ключа кэша и суффикс +nothink в prompt_version.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from svetofor.core.config import LLMMode
from svetofor.llm.backend import ChatRequest, Message, OpenAIBackend
from svetofor.llm.cache import MemoryCache, cache_key
from svetofor.llm.errors import LLMRequestRejectedError
from svetofor.llm.gateway import LLMGateway
from tests.support.fake_llm import FakeLLMServer, ScriptedBackend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
REQUEST = ChatRequest(
    model="test-model",
    messages=(Message("system", "Отвечай JSON."), Message("user", "Есть ли риск?")),
    max_tokens=100,
    json_schema=SCHEMA,
)


class SimpleAnswer(BaseModel):
    ok: bool


@pytest.fixture
def server() -> Iterator[FakeLLMServer]:
    with FakeLLMServer() as srv:
        yield srv


def test_thinking_false_sends_extra_body_mock_client() -> None:
    """При thinking=False в вызов клиента ушёл extra_body с thinking disabled;
    при True — не ушёл."""
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = '{"ok": true}'
    mock_choice.message.refusal = None
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage.prompt_tokens = 10
    mock_completion.usage.completion_tokens = 5
    mock_client.chat.completions.create.return_value = mock_completion

    backend = OpenAIBackend(
        base_url="http://fake",
        api_key="k",
        timeout_s=5,
        supports_json_schema=False,
        client=mock_client,
    )

    # thinking=False
    backend.complete(replace(REQUEST, thinking=False))
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs.get("extra_body") == {"thinking": {"type": "disabled"}}

    # thinking=True
    mock_client.chat.completions.create.reset_mock()
    backend.complete(replace(REQUEST, thinking=True))
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs


def test_thinking_toggle_false_never_sends_parameter(server: FakeLLMServer) -> None:
    """thinking_toggle=False — параметр не уходит никогда."""
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')
    backend = OpenAIBackend(
        base_url=server.url,
        api_key="",
        timeout_s=5,
        supports_json_schema=False,
        thinking_toggle=False,
    )
    backend.complete(replace(REQUEST, thinking=False))
    assert "thinking" not in server.requests[0]

    backend.complete(replace(REQUEST, thinking=True))
    assert "thinking" not in server.requests[1]


def test_thinking_false_wire_request(server: FakeLLMServer) -> None:
    """Проверка формирования сетевого запроса к OpenAI-совместимому API."""
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')
    backend = OpenAIBackend(
        base_url=server.url,
        api_key="",
        timeout_s=5,
        supports_json_schema=False,
        thinking_toggle=True,
    )
    backend.complete(replace(REQUEST, thinking=False))
    assert server.requests[0].get("thinking") == {"type": "disabled"}

    backend.complete(replace(REQUEST, thinking=True))
    assert "thinking" not in server.requests[1]


def test_bad_request_on_thinking_retries_and_disables_toggle(server: FakeLLMServer) -> None:
    """Провайдер отвечает BadRequestError про thinking -> повтор без параметра, ответ получен,
    второй вызов уже без параметра."""
    server.error(400, "Unknown parameter: 'thinking'")
    server.reply('{"ok": true}')
    server.reply('{"ok": true}')

    backend = OpenAIBackend(
        base_url=server.url,
        api_key="",
        timeout_s=5,
        supports_json_schema=False,
        thinking_toggle=True,
    )
    reply = backend.complete(replace(REQUEST, thinking=False))
    assert reply.content == '{"ok": true}'
    # Первая попытка была с параметром, повтор — без него
    assert server.requests[0].get("thinking") == {"type": "disabled"}
    assert "thinking" not in server.requests[1]
    assert backend.thinking_toggle is False

    # Второй вызов клиента — сразу без параметра
    backend.complete(replace(REQUEST, thinking=False))
    assert len(server.requests) == 3
    assert "thinking" not in server.requests[2]


def test_other_bad_request_is_propagated(server: FakeLLMServer) -> None:
    """Другой BadRequestError (не про thinking) — пробрасывается как раньше."""
    server.error(400, "context length exceeded")
    backend = OpenAIBackend(
        base_url=server.url,
        api_key="",
        timeout_s=5,
        supports_json_schema=False,
        thinking_toggle=True,
    )
    with pytest.raises(LLMRequestRejectedError):
        backend.complete(replace(REQUEST, thinking=False))
    assert len(server.requests) == 1
    assert backend.thinking_toggle is True


def test_cache_key_and_prompt_version_in_gateway() -> None:
    """Ключ кэша для thinking=False отличается от True, а prompt_version результата
    оканчивается на +nothink."""
    scripted = ScriptedBackend('{"ok": true}', '{"ok": true}')
    cache = MemoryCache()
    gw = LLMGateway(
        mode=LLMMode.LIVE,
        model_main="main-model",
        model_fast="fast-model",
        backend=scripted,
        cache=cache,
    )

    res_true = gw.complete_json(
        task="test_thinking",
        model_role="main",
        system="Система",
        user="Вопрос",
        schema=SimpleAnswer,
        prompt_version="check.v1",
        thinking=True,
    )
    assert res_true.prompt_version == "check.v1"
    assert scripted.requests[0].thinking is True

    res_false = gw.complete_json(
        task="test_thinking",
        model_role="main",
        system="Система",
        user="Вопрос",
        schema=SimpleAnswer,
        prompt_version="check.v1",
        thinking=False,
    )
    assert res_false.prompt_version == "check.v1+nothink"
    assert res_false.prompt_version.endswith("+nothink")
    assert scripted.requests[1].thinking is False

    # Ключи кэша отличаются
    messages = (Message("system", "Система"), Message("user", "Вопрос"))
    key_true = cache_key("main-model", messages, "check.v1", SimpleAnswer)
    key_false = cache_key("main-model", messages, "check.v1+nothink", SimpleAnswer)
    assert key_true != key_false
    assert cache.get(key_true) is not None
    assert cache.get(key_false) is not None
