"""Замер поиска без модели: причины промаха, места в поиске, отчёт, запрос правила."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from svetofor.eval.retrieval import best_ranks, miss_reason
from svetofor.eval.retrieval_report import RetrievalItem, RetrievalReport, markdown, save_report
from svetofor.rules.facts import SourceChunk
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import LlmCheckRule
from svetofor.rules.types.llm_check import search_request
from svetofor.search.hybrid import SearchHit, quota_first, rrf_merge

QUOTE = "Выплата аванса по Контракту не предусмотрена"
WITH = "6.3. Выплата  аванса по контракту не предусмотрена."  # регистр и пробелы — как у normalize
OTHER = "Оплата производится в течение 7 рабочих дней."


@pytest.mark.parametrize(
    ("context", "hits", "right_type", "package", "expected"),
    [
        ([WITH], [WITH], [WITH], [WITH], None),
        ([OTHER], [WITH, OTHER], [WITH, OTHER], [WITH, OTHER], "cut_by_budget"),
        ([OTHER], [OTHER], [WITH, OTHER], [WITH, OTHER], "not_ranked"),
        ([OTHER], [OTHER], [OTHER], [WITH, OTHER], "wrong_doc_type"),
        ([OTHER], [OTHER], [OTHER], [OTHER], "not_in_chunks"),
        ([], [], [], [], "not_in_chunks"),
    ],
)
def test_miss_reason(
    context: list[str],
    hits: list[str],
    right_type: list[str],
    package: list[str],
    expected: str | None,
) -> None:
    assert miss_reason(QUOTE, context, hits, right_type, package) == expected


def test_quote_split_between_neighbour_chunks_counts_as_found() -> None:
    # модель видит соседние фрагменты подряд — фраза на их стыке тоже у неё перед глазами
    context = ["6.3. Выплата аванса", "по Контракту не предусмотрена."]
    assert miss_reason(QUOTE, context, [], [], []) is None


def _chunk(text: str) -> SourceChunk:
    return SourceChunk(str(uuid.uuid4()), "doc", "Раздел", 1, 1, text, 0)


def test_best_ranks_takes_the_highest_placed_chunk_with_quote() -> None:
    first, second, other = _chunk(WITH), _chunk(f"Повтор: {WITH}"), _chunk(OTHER)
    hits = [
        SearchHit(uuid.UUID(other.id), 0.9, {"vector:0": 1}),
        SearchHit(uuid.UUID(second.id), 0.5, {"text:0": 7, "vector:0": 9}),
        SearchHit(uuid.UUID(first.id), 0.4, {"vector:0": 3}),
    ]
    assert best_ranks(hits, [first, second, other], QUOTE) == {"vector:0": 3}
    assert best_ranks(hits[:1], [first, second, other], QUOTE) == {}


def _report(items: list[RetrievalItem]) -> RetrievalReport:
    params = {"per_method": 20, "top": None, "budget": 6000, "whole_budget": 12000}
    return RetrievalReport("real", "2026-10-31T12:00:00+00:00", 2, params, items)


def test_report_counts_recall_and_reasons(tmp_path: Path) -> None:
    report = _report(
        [
            RetrievalItem("real-001", "PAY-02", "retrieval", True, None, QUOTE, {"vector:0": 2}),
            RetrievalItem("real-001", "VOL-01", "retrieval", False, "not_ranked", "по мере"),
            RetrievalItem("real-002", "SUB-01", "whole", False, "not_in_chunks", "субподряд"),
        ]
    )
    assert report.recall == pytest.approx(1 / 3)
    assert report.reasons() == {
        "cut_by_budget": 0,
        "not_ranked": 1,
        "wrong_doc_type": 0,
        "not_in_chunks": 1,
    }
    text = markdown(report)
    assert "**0,333** (1 из 3)" in text
    assert "| real-001 | VOL-01 | retrieval | not_ranked | — | по мере |" in text
    assert "PAY-02" not in text  # найденные — не в таблице промахов
    path = save_report(report, tmp_path)
    assert path.name == "2026-10-31_retrieval_real.md"
    assert (tmp_path / "2026-10-31_retrieval_real.json").is_file()


def test_empty_report_has_no_recall() -> None:
    report = _report([])
    assert report.recall is None
    assert "Промахов нет." in markdown(report)


def test_search_request_uses_hints_and_question() -> None:
    rules = load_ruleset_file(
        Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
    ).rules
    rule = next(r for r in rules if isinstance(r, LlmCheckRule) and r.search_hints)
    request = search_request(rule)
    assert request.hints == tuple(rule.search_hints)
    assert request.text.startswith(" ".join(rule.search_hints))
    assert " ".join(rule.question.split()) in request.text


def test_quota_puts_each_list_leaders_before_rrf_order() -> None:
    # «общий» фрагмент средне подходит трём правилам и по RRF выше лидеров отдельных правил
    rankings = {
        "text:0": ["lead0", "x1", "x2", "common"],
        "text:1": ["lead1", "y1", "y2", "common"],
        "text:2": ["z0", "z1", "lead2", "common"],
    }
    merged = rrf_merge(rankings)
    assert merged[0][0] == "common"
    order = [item for item, _, _ in quota_first(merged, 1)]
    assert order[:3] == ["lead0", "lead1", "z0"]
    assert order[3] == "common"  # дальше — прежний порядок RRF
    assert [item for item, _, _ in quota_first(merged, 0)] == [m[0] for m in merged]
