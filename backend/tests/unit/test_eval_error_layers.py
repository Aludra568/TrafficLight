"""Тесты определения причин пропусков и источников лишних замечаний."""

from __future__ import annotations

from svetofor.eval.metrics import (
    EXTRA_REASONS,
    MISSED_REASONS,
    PackageResult,
    PairResult,
    extra_by_package,
    extra_reason,
    extra_reasons,
    missed_by_package,
    missed_reason,
    missed_reasons,
    reasons_by_package,
)
from svetofor.eval.run import markdown, summarize


def test_missed_reasons_all_five_kinds() -> None:
    # 1. Не проверено
    p1 = PairResult("NOT-CHECKED", expected=True, predicted=False, not_checked=True)
    assert missed_reason(p1) == "не проверено"

    # 2. Нужное место не попало к модели (контекст False)
    p2 = PairResult("LLM-MISSED-SEARCH", expected=True, predicted=False, context_ok=False)
    assert missed_reason(p2) == "нужное место не попало к модели"

    # 3. Модель видела место, но не признала риск (контекст True)
    p3 = PairResult("LLM-MISSED-DECISION", expected=True, predicted=False, context_ok=True)
    assert missed_reason(p3) == "модель видела место, но не признала риск"

    # 4. Правило без модели не сработало
    p4 = PairResult("RULE-NO-MODEL", expected=True, predicted=False, is_llm=False)
    assert missed_reason(p4) == "правило без модели не сработало"

    # 5. Причина не определена (нет данных)
    p5a = PairResult("RULE-UNDETERMINED", expected=True, predicted=False)
    assert missed_reason(p5a) == "причина не определена"

    # Смысловое правило без данных о контексте
    p5b = PairResult("LLM-NO-CONTEXT", expected=True, predicted=False, is_llm=True)
    assert missed_reason(p5b) == "причина не определена"


def test_extra_reasons_both_kinds() -> None:
    # 1. Смысловое правило (модель)
    p1 = PairResult("LLM-EXTRA", expected=False, predicted=True, is_llm=True)
    assert extra_reason(p1) == "смысловое правило (модель)"

    # 2. Правило без модели
    p2 = PairResult("THRESHOLD-EXTRA", expected=False, predicted=True, is_llm=False)
    assert extra_reason(p2) == "правило без модели"

    # 3. Данных нет — причина не определена
    p3 = PairResult("UNKNOWN-EXTRA", expected=False, predicted=True)
    assert extra_reason(p3) == "причина не определена"


def test_missed_and_extra_reasons_aggregation() -> None:
    pkg1 = PackageResult(
        "pkg-01",
        pairs=[
            PairResult("R-NC", True, False, not_checked=True),
            PairResult("R-CTX-NO", True, False, context_ok=False),
            PairResult("R-CTX-YES", True, False, context_ok=True),
            PairResult("R-NO-LLM", True, False, is_llm=False),
            PairResult("R-UNDET", True, False),
            PairResult("R-EXTRA-LLM", False, True, is_llm=True),
            PairResult("R-EXTRA-RULE", False, True, is_llm=False),
            PairResult("R-OK", True, True),
        ],
    )
    pkg2 = PackageResult(
        "pkg-02",
        pairs=[
            PairResult("R-NC", True, False, not_checked=True),
            PairResult("R-EXTRA-LLM", False, True, is_llm=True),
        ],
    )

    missed_counts = missed_reasons([pkg1, pkg2])
    assert missed_counts == {
        "не проверено": 2,
        "нужное место не попало к модели": 1,
        "модель видела место, но не признала риск": 1,
        "правило без модели не сработало": 1,
        "причина не определена": 1,
    }

    extra_counts = extra_reasons([pkg1, pkg2])
    assert extra_counts["смысловое правило (модель)"] == 2
    assert extra_counts["правило без модели"] == 1

    by_pkg_missed = missed_by_package([pkg1, pkg2])
    assert len(by_pkg_missed["pkg-01"]) == 5
    assert "R-NC: не проверено" in by_pkg_missed["pkg-01"]
    assert "R-CTX-NO: нужное место не попало к модели" in by_pkg_missed["pkg-01"]
    assert "R-CTX-YES: модель видела место, но не признала риск" in by_pkg_missed["pkg-01"]
    assert "R-NO-LLM: правило без модели не сработало" in by_pkg_missed["pkg-01"]
    assert "R-UNDET: причина не определена" in by_pkg_missed["pkg-01"]
    assert by_pkg_missed["pkg-02"] == ["R-NC: не проверено"]

    by_pkg_extra = extra_by_package([pkg1, pkg2])
    assert "R-EXTRA-LLM: смысловое правило (модель)" in by_pkg_extra["pkg-01"]
    assert "R-EXTRA-RULE: правило без модели" in by_pkg_extra["pkg-01"]
    assert by_pkg_extra["pkg-02"] == ["R-EXTRA-LLM: смысловое правило (модель)"]

    all_by_pkg = reasons_by_package([pkg1, pkg2])
    assert len(all_by_pkg["pkg-01"]) == 7
    assert len(all_by_pkg["pkg-02"]) == 2


def test_summarize_and_markdown_sections() -> None:
    pkg = PackageResult(
        "pkg-sample",
        pairs=[
            PairResult("R1", True, False, not_checked=True),
            PairResult("R2", True, False, context_ok=False),
            PairResult("R3", True, False, context_ok=True),
            PairResult("R4", True, False, is_llm=False),
            PairResult("R5", True, False),
            PairResult("E1", False, True, is_llm=True),
            PairResult("E2", False, True, is_llm=False),
        ],
    )

    summary = summarize([pkg])
    assert "missed_reasons" in summary
    assert "extra_reasons" in summary
    assert "missed_by_package" in summary
    assert "extra_by_package" in summary

    report = {
        "dataset": "synthetic-test",
        "created_at": "2026-10-31T00:00:00",
        "model": "test-model",
        "packages": 1,
        **summary,
    }
    md = markdown(report)

    # Раздел «Почему пропущено»
    assert "### Почему пропущено" in md
    for reason in MISSED_REASONS:
        assert f"- {reason}: 1" in md
    assert "По пакетам:" in md
    assert "pkg-sample: R1: не проверено" in md

    # Раздел «Откуда лишние»
    assert "### Откуда лишние" in md
    for reason in EXTRA_REASONS:
        assert f"- {reason}: 1" in md
    assert "pkg-sample: E1: смысловое правило (модель)" in md
    assert "E2: правило без модели" in md


def test_markdown_clean_package_reports_no_errors() -> None:
    pkg = PackageResult(
        "clean-pkg",
        pairs=[
            PairResult("R1", True, True),
            PairResult("R2", False, False),
        ],
    )
    report = {
        "dataset": "clean-test",
        "created_at": "2026-10-31T00:00:00",
        "model": None,
        "packages": 1,
        **summarize([pkg]),
    }
    md = markdown(report)
    assert "### Почему пропущено" in md
    assert "Пропусков нет." in md
    assert "### Откуда лишние" in md
    assert "Лишних замечаний нет." in md
