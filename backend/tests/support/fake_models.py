"""Детерминированные «модели» для сквозных тестов: отвечают по смыслу запроса, а не по очереди."""

from __future__ import annotations

import json
import math
import re
import zlib
from collections.abc import Callable, Sequence
from typing import Any

from svetofor.core.config import get_settings
from svetofor.llm.backend import ChatReply, ChatRequest

FactsByDocument = dict[str, list[dict[str, Any]]]  # название документа → факты
Violations = dict[str, tuple[str, str]]  # код правила → (цитата, пояснение)


class RoutingBackend:
    """Факты — по названию документа в промпте; вердикты — по кодам правил в промпте.

    Нарушение возвращается, только если его цитата есть среди переданных фрагментов
    (как у честной модели): так тест проверяет и поиск, и проверку цитат.
    """

    def __init__(
        self,
        facts: FactsByDocument,
        violations: Violations,
        disputed: frozenset[str] = frozenset(),
    ) -> None:
        self.facts = facts
        self.violations = violations
        self.disputed = disputed  # правила, с которыми «рецензент» не согласен
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatReply:
        self.requests.append(request)
        user = request.messages[1].content
        if "значения полей карточки закупки" in user:
            label = re.search(r"Извлеки из документа «(.+?)»", user)
            content = json.dumps({"facts": self.facts.get(label.group(1) if label else "", [])})
        elif "Другой аналитик решил" in user:
            items = re.findall(r"^Решение (R\d+) — правило ([A-Z]{2,5}-\d{2,3}):", user, re.M)
            content = json.dumps(
                {
                    "reviews": [
                        {
                            "item_id": item,
                            "reasoning": "Цитата о другом." if code in self.disputed else "Да.",
                            "decision": "rejected" if code in self.disputed else "confirmed",
                        }
                        for item, code in items
                    ]
                }
            )
        elif "Проверь фрагменты" in user:
            content = json.dumps(
                {"results": [self._verdict(code, user) for code in _rule_codes(user)]}
            )
        else:
            content = json.dumps({"doc_type": "other", "confidence": 0.1})
        return ChatReply(
            content, tokens_in=len(user) // 4, tokens_out=len(content) // 4, finish_reason="stop"
        )

    def _verdict(self, code: str, prompt: str) -> dict[str, Any]:
        quote, explanation = self.violations.get(code, ("", ""))
        # ищем только в тексте документов: в описании правил бывают примеры формулировок
        documents = re.findall(r"<<<ДОКУМЕНТ\n(.*?)\nДОКУМЕНТ>>>", prompt, flags=re.DOTALL)
        flat = " ".join(" ".join(documents).split())
        if quote and " ".join(quote.split()) in flat:
            return {
                "rule_id": code,
                "verdict": "violation",
                "chunk_id": "F1",
                "quote": quote,
                "explanation": explanation,
            }
        return {
            "rule_id": code,
            "verdict": "not_found",
            "chunk_id": None,
            "quote": None,
            "explanation": "",
        }


def _rule_codes(prompt: str) -> list[str]:
    return re.findall(r"^Правило ([A-Z]{2,5}-\d{2,3}):", prompt, flags=re.MULTILINE)


class StemEmbedder:
    """Мешок основ слов в векторе фиксированной размерности: похожие тексты — близкие векторы."""

    def __init__(self, dim: int | None = None, fail: bool = False) -> None:
        self.dim = dim or get_settings().embed_dim
        self.fail = fail
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in re.findall(r"[а-яёa-z0-9]{3,}", text.lower()):
            vector[zlib.crc32(word[:5].encode()) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            from svetofor.search.embeddings import EmbeddingError

            raise EmbeddingError("недоступен")
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_passages([text])[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


def fact(key: str, value: Any, quote: str, unit: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "raw": quote,
        "quote": quote,
        "chunk_id": "F1",
    }


Factory = Callable[[], Any]
