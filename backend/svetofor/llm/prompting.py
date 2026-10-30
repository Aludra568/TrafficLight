"""Промпты из шаблонов `prompts/*.vN.md`.

Версия шаблона входит в ключ кэша и в отчёт. Текст документа попадает в промпт только
через фильтр `doc`: он гасит маркеры `<<<` и `>>>`, чтобы документ не мог «закрыть» блок
ДОКУМЕНТ>>> и дописать свои указания (защита от prompt injection, раздел 7).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from svetofor.rules.verify import strip_invisible

PROMPTS_DIR = Path(__file__).parent / "prompts"
SYSTEM_TEMPLATE = "system.v1"
_ALIAS = re.compile(r"^\[?\s*[FfФф]?\s*(\d{1,3})\s*\]?$")


def escape_document(text: str) -> str:
    """Текст документа для промпта: без невидимых символов (теги Unicode несут скрытые указания
    модели) и без маркеров блока. Цитаты сверяются с нормализованным текстом, где невидимых
    символов тоже нет, поэтому проверка цитат от этого не страдает."""
    return strip_invisible(text).replace("<<<", "‹‹‹").replace(">>>", "›››")


def squash(text: str) -> str:
    """Поля правил из YAML (блоки `>`) — в одну строку без лишних пробелов и переводов."""
    return " ".join(str(text).split())


# Промпты — простой текст, не HTML: HTML-экранирование исказило бы цитаты.
# Текст документа экранирует фильтр `doc`.
_ENV = Environment(
    loader=FileSystemLoader(PROMPTS_DIR),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["doc"] = escape_document
_ENV.filters["squash"] = squash


@dataclass(frozen=True)
class Budgets:
    """Сколько текста документа (в токенах) класть в промпт при данной длине контекста модели.

    Оценка токенов по символам (3,5 на токен) у разных моделей ошибается в обе стороны, поэтому
    запас большой; если провайдер всё же ответит «длиннее контекста», текст сокращается вдвое.
    """

    retrieval: int  # найденные поиском фрагменты для группы правил
    whole: int  # документ целиком: карточка закупки, второй проход «условие не найдено»

    @classmethod
    def for_context(cls, context_tokens: int) -> Budgets:
        return cls(min(6000, context_tokens // 4), min(12_000, context_tokens // 2))


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    version: str  # «check_group.v1+system.v1» — в ключ кэша и в отчёт


@dataclass(frozen=True)
class PromptChunk:
    alias: str  # «F1»: короткий номер вместо UUID — модели проще скопировать без ошибок
    source_id: str  # id фрагмента в БД
    section_path: str
    page_from: int
    page_to: int
    text: str


def alias_chunks(chunks: Sequence[tuple[str, str, int, int, str]]) -> list[PromptChunk]:
    """(id, путь раздела, стр. с, стр. по, текст) → фрагменты с номерами F1, F2, …"""
    return [
        PromptChunk(f"F{i}", source_id, path, page_from, page_to, text)
        for i, (source_id, path, page_from, page_to, text) in enumerate(chunks, start=1)
    ]


def resolve_alias(alias: str | None, chunks: Sequence[PromptChunk]) -> PromptChunk | None:
    """Номер фрагмента из ответа модели → фрагмент. Терпит «[F3]», «f3», «3», «Ф3»."""
    if not alias:
        return None
    match = _ALIAS.match(alias.strip())
    if not match:
        return None
    wanted = f"F{int(match.group(1))}"
    return next((c for c in chunks if c.alias == wanted), None)


def render(template: str, **context: Any) -> Prompt:
    system = _ENV.get_template(f"{SYSTEM_TEMPLATE}.md").render()
    user = _ENV.get_template(f"{template}.md").render(**context)
    return Prompt(system.strip(), _tidy(user), f"{template}+{SYSTEM_TEMPLATE}")


def _tidy(text: str) -> str:
    """Не больше одной пустой строки подряд — шаблоны с циклами оставляют лишние."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()
