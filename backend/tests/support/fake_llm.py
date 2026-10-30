"""Поддельные провайдеры LLM для тестов: без сети, с записью запросов.

`FakeLLMServer` — настоящий HTTP-сервер на 127.0.0.1 с OpenAI-совместимым API: проверяет
адаптер вместе с SDK (повторы, заголовки, ошибки). `ScriptedBackend` — то же на уровне
протокола `ChatBackend`, для быстрых тестов шлюза.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from svetofor.llm.backend import ChatReply, ChatRequest
from svetofor.llm.errors import LLMError


def completion(
    content: str, finish_reason: str = "stop", usage: bool = True, refusal: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    return body


class FakeLLMServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []  # заголовки запросов, имена в нижнем регистре
        self._responses: deque[tuple[int, dict[str, Any], dict[str, str]]] = deque()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                server.headers.append({k.lower(): v for k, v in self.headers.items()})
                server.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                if server._responses:
                    status, body, headers = server._responses.popleft()
                else:
                    status, body, headers = 500, {"error": {"message": "no scripted reply"}}, {}
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def reply(self, content: str, **kw: Any) -> None:
        self._responses.append((200, completion(content, **kw), {}))

    def error(
        self, status: int, message: str = "error", headers: dict[str, str] | None = None
    ) -> None:
        self._responses.append((status, {"error": {"message": message}}, headers or {}))

    def __enter__(self) -> FakeLLMServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class Truncated(str):
    """Ответ, оборванный по лимиту длины (finish_reason="length")."""


class ScriptedBackend:
    """Отвечает по списку: строка — ответ модели, `Truncated` — оборванный, исключение — сбой."""

    def __init__(self, *script: str | LLMError, finish_reason: str = "stop") -> None:
        self.script = deque(script)
        self.requests: list[ChatRequest] = []
        self.finish_reason = finish_reason

    def complete(self, request: ChatRequest) -> ChatReply:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("неожиданный вызов модели")
        item = self.script.popleft()
        if isinstance(item, LLMError):
            raise item
        reason = "length" if isinstance(item, Truncated) else self.finish_reason
        return ChatReply(str(item), tokens_in=100, tokens_out=20, finish_reason=reason)
