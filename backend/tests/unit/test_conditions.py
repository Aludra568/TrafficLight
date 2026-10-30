import pytest

from svetofor.rules.conditions import ConditionError, parse_condition


@pytest.mark.parametrize(
    ("source", "value", "expected"),
    [
        ("value > 7", 30, True),
        ("value > 7", 7, False),
        ("value >= 7", 7, True),
        ("value < 5", 4.5, True),
        ("value <= 20", 20, True),
        ("value == 0", 0, True),
        ("value != 0", 0, False),
        ("value >= 10 and value <= 30", 10, True),
        ("value >= 10 and value <= 30", 31, False),
        ("value < 5 or value > 60", 70, True),
        ("value < 5 or value > 60", 30, False),
        # and связывает сильнее or: (v < 5) or (v > 10 and v < 20)
        ("value < 5 or value > 10 and value < 20", 15, True),
        ("value < 5 or value > 10 and value < 20", 25, False),
        ("value == 'ban'", "ban", True),
        ("value == 'ban'", "restriction", False),
        ("value != 'none'", "ban", True),
        ("value == true", True, True),
        ("value == false", True, False),
        ("value > 0,5", 1, True),  # десятичная запятая
        ("  value>7  ", 8, True),
    ],
)
def test_evaluate(source: str, value: float | str | bool, expected: bool) -> None:
    assert parse_condition(source).evaluate(value) is expected


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("value > 7", "30"),  # число против строки — не совпадает, а не исключение
        ("value == 'ban'", 1),
        ("value > 'a'", "b"),  # сравнение строк на больше/меньше запрещено
        ("value > true", True),
        ("value == 1", True),  # bool не равен 1
    ],
)
def test_type_mismatch_is_false(source: str, value: float | str | bool) -> None:
    assert parse_condition(source).evaluate(value) is False


@pytest.mark.parametrize(
    ("source", "message_part"),
    [
        ("", "пустое"),
        ("value >", "оборвано"),
        ("value 7", "операция"),
        ("x > 7", "value"),
        ("value > 7 and", "ещё одно сравнение"),
        ("value > 7 xor value < 3", "and/or"),
        ("value > 7; import os", "Непонятный фрагмент"),
        ("value > __import__", "Ожидалось число"),
        ("value > (7)", "Непонятный фрагмент"),
    ],
)
def test_rejects_bad_input_with_russian_message(source: str, message_part: str) -> None:
    with pytest.raises(ConditionError, match=message_part):
        parse_condition(source)


def test_literals_exposed_for_validation() -> None:
    assert parse_condition("value >= 10 and value <= 30 or value == 'x'").literals == [
        10.0,
        30.0,
        "x",
    ]
