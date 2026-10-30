"""Ошибки слоя LLM.

У каждой ошибки есть `kind` (для метрик и журнала) и сообщение по-русски: оно попадает в отчёт
как причина «не проверено» — без выдуманных результатов.
"""

from __future__ import annotations


class LLMError(Exception):
    kind = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LLMUnavailableError(LLMError):
    """Модель недоступна: сеть, ключ, лимиты провайдера, ошибка сервера."""

    kind = "unavailable"


class LLMRateLimitedError(LLMUnavailableError):
    """Провайдер ограничил частоту запросов (429): шлюз ждёт и повторяет, прежде чем сдаться."""

    kind = "rate_limited"


class LLMRequestRejectedError(LLMError):
    """Провайдер отклонил запрос (слишком длинный контекст, фильтр содержимого)."""

    kind = "rejected"


class LLMContextTooLongError(LLMRequestRejectedError):
    """Промпт длиннее контекста модели. Вызывающий код может сократить текст и повторить."""

    kind = "context"


class LLMReplayMissError(LLMError):
    """Режим replay: записанного ответа нет, а ходить к модели нельзя."""

    kind = "replay_miss"


class LLMSchemaError(LLMError):
    """Ответ не прошёл проверку по схеме даже после попытки исправления."""

    kind = "schema"


class LLMTruncatedError(LLMSchemaError):
    """Ответ оборвался по лимиту длины. Вызывающий код может разделить задачу на части."""

    kind = "truncated"
