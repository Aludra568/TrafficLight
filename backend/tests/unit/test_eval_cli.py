"""Команда записи кассеты без ключа модели: понятное сообщение и код 2, а не трассировка."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from svetofor.core.config import get_settings
from svetofor.eval.__main__ import _cassette


def test_cassette_without_model_explains_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # без main(): он перенастраивает журнал всего процесса, и это ломало соседние тесты
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model_main", "")
    target = tmp_path / "tape.jsonl"
    target.write_text('{"key": "old"}\n', encoding="utf-8")
    assert _cassette(argparse.Namespace(package=None, out=str(target))) == 2
    assert "Нужна модель" in capsys.readouterr().err
    assert target.exists()  # старая кассета не стёрта, пока записывать нечем


def test_incomplete_cassette_is_reported_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Сбойный вызов модели в кассету не попадает — запись «успешна», а демо без ключа
    показывает правила «не проверено» (20.10.2026). Команда обязана об этом сказать."""
    from svetofor.eval import cassette

    monkeypatch.setattr(cassette, "record", lambda settings, package, target: (12, "red", 3))
    assert _cassette(argparse.Namespace(package=None, out=str(tmp_path / "tape.jsonl"))) == 3
    assert "Кассета неполная: 3 правил" in capsys.readouterr().err
