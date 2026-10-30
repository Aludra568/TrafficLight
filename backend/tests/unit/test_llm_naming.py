"""Имя модели наружу — без ID каталога облака."""

from __future__ import annotations

import pytest

from svetofor.llm.naming import public_model_name


@pytest.mark.parametrize(
    ("model", "shown"),
    [
        ("gpt://b1gexamplefolder/model-x", "model-x"),
        ("gpt://b1gexamplefolder/model-y/latest", "model-y/latest"),
        ("qwen2.5:3b", "qwen2.5:3b"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("", None),
        (None, None),
    ],
)
def test_public_model_name(model: str | None, shown: str | None) -> None:
    assert public_model_name(model) == shown
