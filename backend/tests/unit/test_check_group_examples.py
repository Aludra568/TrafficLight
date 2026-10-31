"""Тесты пограничных примеров правил в шаблоне check_group.v4."""

from __future__ import annotations

from pathlib import Path

from svetofor.llm.prompting import render
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.model import LlmCheckRule

RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
RULES = {r.id: r for r in load_ruleset_file(RULES_FILE).rules}


def _dummy_chunk(
    text: str = "1.1. Поставка товара осуществляется в соответствии с условиями.",
) -> object:
    return type(
        "C",
        (),
        {"alias": "F1", "section_path": "Раздел 1", "page_from": 1, "page_to": 1, "text": text},
    )()


def test_render_v4_with_examples() -> None:
    rule = RULES["TRM-01"]
    assert isinstance(rule, LlmCheckRule)
    assert len(rule.examples) > 0

    prompt = render("check_group.v4", rules=[rule], chunks=[_dummy_chunk()])
    assert f"Примеры к правилу {rule.id}" in prompt.user
    for example in rule.examples:
        assert example.text in prompt.user
    assert "нарушение" in prompt.user
    assert "нормально" in prompt.user


def test_render_v4_without_examples_matches_v3() -> None:
    rule = RULES["TRM-02"]
    assert isinstance(rule, LlmCheckRule)
    assert rule.examples == []

    chunks = [_dummy_chunk()]
    prompt_v3 = render("check_group.v3", rules=[rule], chunks=chunks)
    prompt_v4 = render("check_group.v4", rules=[rule], chunks=chunks)
    assert prompt_v4.user == prompt_v3.user


def test_render_v4_rule_cleared_examples_matches_v3() -> None:
    rule = RULES["TRM-01"]
    assert isinstance(rule, LlmCheckRule)
    rule_no_examples = rule.model_copy(update={"examples": []})

    chunks = [_dummy_chunk()]
    prompt_v3 = render("check_group.v3", rules=[rule_no_examples], chunks=chunks)
    prompt_v4 = render("check_group.v4", rules=[rule_no_examples], chunks=chunks)
    assert prompt_v4.user == prompt_v3.user
