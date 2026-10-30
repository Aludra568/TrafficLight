"""Метрики и бутстрэп на игрушечных данных — значения посчитаны вручную."""

from __future__ import annotations

import pytest

from svetofor.eval.bootstrap import bootstrap_ci
from svetofor.eval.metrics import (
    FactResult,
    PackageResult,
    PairResult,
    channel_conflicts,
    confusion,
    context_recall,
    facts_accuracy,
    facts_accuracy_model,
    facts_accuracy_rules,
    location_match,
    per_rule,
    percentile,
    precision,
    quotes_verified,
    recall,
    reliability,
    timing,
)

A = PackageResult(
    "a",
    pairs=[
        PairResult("PAY-01", True, True, location_ok=True),  # TP, место верное
        PairResult("LIA-03", True, False),  # FN
        PairResult("TRM-01", False, True),  # FP
        PairResult("VOL-01", False, False),  # TN
    ],
    facts=[
        FactResult("payment_term_working_days", 15, 15.0),
        FactResult("advance_percent", 0, 30.0),
    ],
    seconds=40,
    quotes_shown=3,
    quotes_rejected=1,
)
B = PackageResult(
    "b",
    pairs=[
        PairResult("PAY-01", True, True, location_ok=False),  # TP, место не то
        PairResult("LIA-03", True, False, not_checked=True),  # «не проверено» = пропуск
        PairResult("TRM-01", False, False),
    ],
    facts=[FactResult("national_regime", "ban", "ban")],
    seconds=100,
    failed=True,
)


def test_confusion_and_rates() -> None:
    assert confusion(A.pairs + B.pairs) == confusion([*A.pairs, *B.pairs])
    c = confusion([*A.pairs, *B.pairs])
    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 2, 2)
    assert recall([A, B]) == 0.5 and precision([A, B]) == pytest.approx(2 / 3)
    assert location_match([A, B]) == 0.5
    assert facts_accuracy([A, B]) == pytest.approx(2 / 3)
    assert quotes_verified([A, B]) == 0.75 and reliability([A, B]) == 0.5


def test_empty_cases_are_none_not_zero() -> None:
    empty = PackageResult("x")
    assert recall([empty]) is None and precision([empty]) is None
    assert location_match([empty]) is None and quotes_verified([empty]) is None


def test_channels_scored_separately() -> None:
    result = PackageResult(
        "c",
        facts=[
            FactResult("payment_term_working_days", 15, 15.0, 15.0, 15.0),  # оба верно
            FactResult("delivery_term_days", 10, 10.0, 10.0, None),  # модель не нашла
            # каналы разошлись, показано значение модели — и оно неверное
            FactResult("advance_percent", 0, 30.0, 0.0, 30.0, conflict=True),
        ],
    )
    assert facts_accuracy([result]) == pytest.approx(2 / 3)
    assert facts_accuracy_rules([result]) == 1.0
    assert facts_accuracy_model([result]) == pytest.approx(1 / 3)
    assert channel_conflicts([result]) == {"total": 1, "model_right": 0, "rules_right": 1}


def test_context_recall_ignores_pairs_without_search() -> None:
    result = PackageResult(
        "d",
        pairs=[
            PairResult("LIA-03", True, True, context_ok=True),
            PairResult("TRM-01", True, False, context_ok=False),  # поиск не дал фрагмент
            PairResult("PAY-01", True, True),  # пороговое правило — поиска нет
        ],
    )
    assert context_recall([result]) == 0.5
    assert context_recall([PackageResult("e")]) is None


def test_per_rule_table() -> None:
    table = per_rule([A, B])
    assert table["PAY-01"]["tp"] == 2 and table["PAY-01"]["recall"] == 1.0
    assert table["LIA-03"]["fn"] == 2 and table["LIA-03"]["not_checked"] == 1
    assert table["TRM-01"]["precision"] == 0.0


def test_timing_percentiles() -> None:
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert timing([A, B])["p95"] == pytest.approx(97)
    assert percentile([], 0.5) is None


def test_bootstrap_is_reproducible_and_brackets_estimate() -> None:
    results = [PackageResult(str(i), pairs=[PairResult("R", True, i % 4 != 0)]) for i in range(40)]
    first = bootstrap_ci(results, recall)
    assert first == bootstrap_ci(results, recall)  # фиксированное зерно
    assert first is not None and first[0] <= 0.75 <= first[1]
    assert bootstrap_ci([], recall) is None
    perfect = [PackageResult(str(i), pairs=[PairResult("R", True, True)]) for i in range(5)]
    assert bootstrap_ci(perfect, recall) == (1.0, 1.0)


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (1, "1 пакет"),
        (3, "3 пакета"),
        (5, "5 пакетов"),
        (11, "11 пакетов"),
        (21, "21 пакет"),
        (12, "12 пакетов"),
    ],
)
def test_plural(n: int, text: str) -> None:
    from svetofor.eval.run import plural

    assert plural(n, ("пакет", "пакета", "пакетов")) == text
