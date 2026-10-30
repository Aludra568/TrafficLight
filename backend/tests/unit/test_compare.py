"""Сравнение редакций: пункты по номеру и тексту, перенумерация, слова, абзацы без номеров."""

from __future__ import annotations

from svetofor.reports.compare import Unit, compare_units, word_diff


def u(key: str | None, text: str) -> Unit:
    return Unit(key, f"Контракт › {key}", text, 1)


def test_changed_added_removed_renumbered() -> None:
    old = [
        u("2.1", "2.1. Оплата производится в течение 15 рабочих дней."),
        u("3.1", "3.1. Срок поставки — 10 рабочих дней."),
        u("4.1", "4.1. Пеня 1/300 ключевой ставки."),
        u("5.1", "5.1. Споры — в арбитражном суде."),
    ]
    new = [
        u("2.1", "2.1. Оплата производится в течение 30 рабочих дней."),
        u("3.1", "3.1. Срок поставки — 10 рабочих дней."),  # без изменений — не показываем
        u("4.2", "4.2. Пеня 1/300 ключевой ставки."),  # тот же текст под другим номером
        u("6.1", "6.1. Поставщик вывозит тару за свой счёт."),
    ]
    changes = {(c.status, (c.old or c.new).key): c for c in compare_units(old, new)}  # type: ignore[union-attr]
    assert set(changes) == {
        ("changed", "2.1"),
        ("renumbered", "4.1"),
        ("removed", "5.1"),
        ("added", "6.1"),
    }
    renumbered = changes[("renumbered", "4.1")]
    assert renumbered.new is not None and renumbered.new.key == "4.2"
    diff = changes[("changed", "2.1")].diff
    assert {"op": "delete", "text": "15"} in diff and {"op": "insert", "text": "30"} in diff


def test_word_diff_keeps_context() -> None:
    diff = word_diff("срок 10 рабочих дней", "срок 10 календарных дней")
    assert diff == [
        {"op": "equal", "text": "срок 10"},
        {"op": "delete", "text": "рабочих"},
        {"op": "insert", "text": "календарных"},
        {"op": "equal", "text": "дней"},
    ]


def test_paragraphs_without_numbers() -> None:
    old = [u(None, "Поставка бумаги."), u(None, "Оплата по факту."), u(None, "Гарантия год.")]
    new = [u(None, "Поставка бумаги."), u(None, "Оплата авансом 30 %."), u(None, "Гарантия год.")]
    (change,) = compare_units(old, new)
    assert change.status == "changed" and change.old and change.old.text == "Оплата по факту."
