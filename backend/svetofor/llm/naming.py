"""Имя модели для отчётов, интерфейса и результатов оценки — без адреса облака."""

from __future__ import annotations

import re

# у части облаков модель — <схема>://<ID каталога>/<имя>: ID каталога — данные облачного
# аккаунта, в отчёт для поставщика, в репозиторий с результатами оценки и жюри он не нужен
_URI_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+/")


def public_model_name(model: str | None) -> str | None:
    """«gpt://b1g…/model-x» → «model-x»; обычные имена («qwen2.5:3b») — как есть."""
    if not model:
        return None
    return _URI_PREFIX.sub("", model) or None
