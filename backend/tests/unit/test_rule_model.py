"""Модель языка правил: стартовый набор валиден, ошибки методолога ловятся с понятным текстом."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from svetofor.rules.loader import (
    RulesetError,
    dump_ruleset,
    load_ruleset_file,
    load_ruleset_text,
    parse_ruleset,
)
from svetofor.rules.model import LlmCheckRule, PatternRule, ThresholdRule
from svetofor.rules.templates import TemplateError, render
from tests.conftest import REPO_DIR

STARTER = REPO_DIR / "rules" / "44fz_supplier_base.yaml"

BASE: dict[str, Any] = {
    "id": "TST-01",
    "title": "Тестовое правило",
    "category": "payment",
    "law": ["44-ФЗ"],
    "applies_to": ["contract"],
    "message": "Значение {value}",
    "why_risk": "Потому что",
    "recommendation": "Сделать что-то",
}


def ruleset(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"ruleset": {"code": "test-set", "title": "Тест", "law": "44-ФЗ"}, "rules": list(rules)}


def threshold(**over: Any) -> dict[str, Any]:
    return {
        **BASE,
        "type": "threshold",
        "fact": "payment_term_working_days",
        "conditions": [{"when": "value > 7", "severity": "yellow"}],
        **over,
    }


def llm(**over: Any) -> dict[str, Any]:
    return {
        **BASE,
        "type": "llm_check",
        "group": "payment",
        "question": "Есть ли что-то важное?",
        "violation_if": "есть нарушение",
        "ok_if": "всё хорошо",
        "search_hints": ["оплата"],
        "severity": "red",
        **over,
    }


def problems_of(raw: dict[str, Any]) -> str:
    with pytest.raises(RulesetError) as info:
        parse_ruleset(raw)
    return "\n".join(info.value.problems)


class TestStarterRuleset:
    def test_loads_and_counts(self) -> None:
        rs = load_ruleset_file(STARTER)
        assert rs.ruleset.code == "44fz-supplier-base"
        assert len(rs.rules) == 39
        kinds = {type(r) for r in rs.rules}
        assert {PatternRule, ThresholdRule, LlmCheckRule} <= kinds

    def test_all_draft_until_lawyer_checks(self) -> None:
        assert all(r.status.value == "draft" for r in load_ruleset_file(STARTER).rules)

    def test_dump_and_reload_roundtrip(self) -> None:
        rs = load_ruleset_file(STARTER)
        again = load_ruleset_text(dump_ruleset(rs))
        assert again.model_dump() == rs.model_dump()
        assert "Срок оплаты" in dump_ruleset(rs)  # кириллица не экранируется


class TestValidation:
    def test_valid_minimal_rules(self) -> None:
        rs = parse_ruleset(ruleset(threshold(), llm(id="TST-02")))
        assert [r.id for r in rs.rules] == ["TST-01", "TST-02"]

    def test_unknown_fact(self) -> None:
        assert "Неизвестный факт «payment_days»" in problems_of(
            ruleset(threshold(fact="payment_days"))
        )

    def test_catastrophic_regex_rejected(self) -> None:
        """«(а+)+» на длинном документе думает минутами — такое выражение не сохраняется."""
        from svetofor.rules.model import nested_quantifier

        assert nested_quantifier(r"(\w+)+$") and nested_quantifier(r"(?:\s*\d+)+")
        assert not nested_quantifier(r"(?:\s*\([^)]{1,60}\))?")  # повтор внутри, снаружи «?»
        assert not nested_quantifier(r"([a+]b)+")  # «+» внутри класса символов — не повтор

    def test_slow_regex_timed_on_save_not_on_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Замер времени выражения — при записи правила (редактор, импорт). Сохранённое правило
        читается без замера: под нагрузкой (8 потоков worker-llm, OCR) замер ложно «не
        укладывался», и падала вся группа правил (Docker, 20.10.2026). Во время работы правило
        и так ограничено временем поиска (`TimeBudget`)."""
        from svetofor.rules import model
        from svetofor.rules.loader import parse_rule
        from svetofor.rules.store import rule_body, rule_from_body

        body = {**BASE, "type": "pattern", "patterns": ["(?i)казначейск"], "severity": "yellow"}
        stored = rule_body(parse_rule(body))
        monkeypatch.setattr(model, "too_slow", lambda pattern: True)  # «машина под нагрузкой»
        with pytest.raises(RulesetError, match="работает слишком долго"):
            parse_rule(body)
        assert rule_from_body(stored).code == "TST-01"
        broken = {**stored, "patterns": ["(?i)казначейск("]}  # синтаксис проверяется и при чтении
        with pytest.raises(ValueError, match="Ошибка в регулярном выражении"):
            rule_from_body(broken)

    def test_number_fact_with_string_literal(self) -> None:
        bad = threshold(conditions=[{"when": "value == 'ban'", "severity": "red"}])
        assert "не число" in problems_of(ruleset(bad))

    def test_enum_fact_unknown_value(self) -> None:
        bad = threshold(
            fact="national_regime", conditions=[{"when": "value == 'запрет'", "severity": "red"}]
        )
        assert "допустимы значения: 'ban'" in problems_of(ruleset(bad))

    def test_text_fact_has_no_threshold(self) -> None:
        bad = threshold(fact="penalty_rate", conditions=[{"when": "value > 1", "severity": "red"}])
        assert "порог к нему не применим" in problems_of(ruleset(bad))

    def test_bad_condition_syntax_mentions_rule(self) -> None:
        bad = threshold(conditions=[{"when": "value >> 7", "severity": "red"}])
        text = problems_of(ruleset(bad))
        assert text.startswith("Правило TST-01, поле conditions → when:")

    def test_bad_regex(self) -> None:
        bad = {**BASE, "type": "pattern", "patterns": ["(незакрытая"], "severity": "yellow"}
        assert "Ошибка в регулярном выражении" in problems_of(ruleset(bad))

    def test_absent_report_needs_message(self) -> None:
        assert "absent_message" in problems_of(ruleset(llm(absent_behavior="report")))

    def test_verified_needs_who_and_when(self) -> None:
        assert "кто проверил" in problems_of(ruleset(threshold(status="verified")))
        ok = threshold(status="verified", verified_by="Юрист И.И.", verified_at="2026-10-05")
        assert parse_ruleset(ruleset(ok)).rules[0].status.value == "verified"

    def test_duplicate_ids(self) -> None:
        assert "Повторяются коды правил: TST-01" in problems_of(ruleset(threshold(), llm()))

    def test_bad_id_format(self) -> None:
        assert "id" in problems_of(ruleset(threshold(id="pay-1")))

    def test_unknown_field_forbidden(self) -> None:
        assert "colour" in problems_of(ruleset(threshold(colour="red")))

    def test_unsafe_template_rejected(self) -> None:
        assert "простые подстановки" in problems_of(ruleset(threshold(message="{value.__class__}")))

    def test_cross_document_needs_two_different_types(self) -> None:
        bad = {
            **BASE,
            "type": "cross_document",
            "fact": "delivery_term_days",
            "compare": ["spec", "spec"],
            "severity": "red",
        }
        assert "Сравниваются два разных типа документов" in problems_of(ruleset(bad))

    def test_unknown_type(self) -> None:
        assert "type" in problems_of(ruleset({**BASE, "type": "magic"}))

    def test_not_a_mapping(self) -> None:
        with pytest.raises(RulesetError, match="разделы ruleset и rules"):
            parse_ruleset(["a", "b"])

    def test_broken_yaml(self) -> None:
        with pytest.raises(RulesetError, match="не читается как YAML"):
            load_ruleset_text("ruleset: [unclosed")

    def test_errors_do_not_mutate_input(self) -> None:
        raw = ruleset(threshold(fact="nope"))
        before = copy.deepcopy(raw)
        with pytest.raises(RulesetError):
            parse_ruleset(raw)
        assert raw == before

    def test_rule_with_two_examples_valid(self) -> None:
        ex1 = {
            "text": "Заказчик вправе отказаться при просрочке любой партии.",
            "verdict": "violation",
            "why": "Разовая просрочка не является существенным нарушением.",
        }
        ex2 = {
            "text": "Отказ допускается по основаниям ст. 95 Закона № 44-ФЗ.",
            "verdict": "ok",
            "why": "Законные основания отказа являются стандартным условием.",
        }
        rs = parse_ruleset(ruleset(llm(examples=[ex1, ex2])))
        rule = rs.rules[0]
        assert isinstance(rule, LlmCheckRule)
        assert len(rule.examples) == 2
        assert rule.examples[0].text == ex1["text"]
        assert rule.examples[0].verdict == "violation"
        assert rule.examples[0].why == ex1["why"]
        assert rule.examples[1].text == ex2["text"]
        assert rule.examples[1].verdict == "ok"
        assert rule.examples[1].why == ex2["why"]

    def test_example_invalid_verdict_rejected(self) -> None:
        raw = llm(
            examples=[
                {
                    "text": "Заказчик вправе отказаться при просрочке любой партии.",
                    "verdict": "maybe",
                    "why": "Непонятный вердикт.",
                }
            ]
        )
        assert "verdict" in problems_of(ruleset(raw))

    def test_too_many_examples_rejected(self) -> None:
        example = {
            "text": "Отказ допускается по основаниям ст. 95 Закона № 44-ФЗ.",
            "verdict": "ok",
            "why": "Законные основания отказа являются стандартным условием.",
        }
        raw = llm(examples=[example] * 7)
        assert "examples" in problems_of(ruleset(raw))

    def test_example_extra_field_forbidden(self) -> None:
        raw = llm(
            examples=[
                {
                    "text": "Отказ допускается по основаниям ст. 95 Закона № 44-ФЗ.",
                    "verdict": "ok",
                    "why": "Законные основания отказа являются стандартным условием.",
                    "extra": "forbidden",
                }
            ]
        )
        assert "extra" in problems_of(ruleset(raw))

    def test_rule_without_examples_defaults_to_empty_list(self) -> None:
        rs = parse_ruleset(ruleset(llm()))
        rule = rs.rules[0]
        assert isinstance(rule, LlmCheckRule)
        assert rule.examples == []


class TestTemplates:
    def test_render(self) -> None:
        assert render("Оплата через {value} раб. дн.", value="30") == "Оплата через 30 раб. дн."

    def test_unknown_field_kept(self) -> None:
        assert (
            render("ТЗ: {spec}, контракт: {contract}", spec="10") == "ТЗ: 10, контракт: {contract}"
        )

    @pytest.mark.parametrize(
        "bad", ["{value.__class__}", "{value[0]}", "{value!r}", "{value:>10}", "{0}"]
    )
    def test_unsafe_rejected(self, bad: str) -> None:
        with pytest.raises(TemplateError):
            render(bad, value="x")
