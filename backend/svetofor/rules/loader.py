"""Загрузка и выгрузка наборов правил в YAML с понятными ошибками для методолога."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from svetofor.rules.model import Rule, RulesetFile

_RULE: TypeAdapter[Rule] = TypeAdapter(Rule)
_TYPE_HINT = (
    "укажите type: pattern, threshold, llm_check, cross_document, internal_consistency или signal"
)


class RulesetError(ValueError):
    """Набор правил не прошёл проверку. `problems` — по одной строке на ошибку."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Ошибки в наборе правил:\n" + "\n".join(f"• {p}" for p in problems))


def _rule_label(raw: dict[str, Any], index: int) -> str:
    rules = raw.get("rules")
    if isinstance(rules, list) and index < len(rules) and isinstance(rules[index], dict):
        return str(rules[index].get("id") or f"№{index + 1}")
    return f"№{index + 1}"


def _describe(raw: dict[str, Any], error: Any) -> str:
    loc = list(error.get("loc", ()))
    message = str(error.get("msg", "")).removeprefix("Value error, ")
    if len(loc) >= 2 and loc[0] == "rules" and isinstance(loc[1], int):
        where = f"Правило {_rule_label(raw, loc[1])}"
        # loc[2] — имя варианта дискриминатора (pattern/threshold/…), пользователю не нужно
        rest = [str(p) for p in loc[3:] if not isinstance(p, int)] if len(loc) > 2 else []
        return f"{where}, поле {' → '.join(rest)}: {message}" if rest else f"{where}: {message}"
    if loc:
        return f"{' → '.join(str(p) for p in loc)}: {message}"
    return message


def parse_ruleset(raw: Any) -> RulesetFile:
    if not isinstance(raw, dict):
        raise RulesetError(["Файл должен содержать разделы ruleset и rules"])
    try:
        return RulesetFile.model_validate(raw)
    except ValidationError as exc:
        raise RulesetError([_describe(raw, e) for e in exc.errors()]) from exc


def parse_rule(raw: Any) -> Rule:
    """Одно правило (редактор в интерфейсе) — с теми же понятными ошибками, что и набор."""
    if not isinstance(raw, dict):
        raise RulesetError(["Правило должно быть объектом с полями id, title, type и другими"])
    try:
        return _RULE.validate_python(raw)
    except ValidationError as exc:
        wrapped = {"rules": [raw]}
        problems = []
        for error in exc.errors():
            if error.get("type") in ("union_tag_not_found", "union_tag_invalid"):
                problems.append(f"Правило {_rule_label(wrapped, 0)}: {_TYPE_HINT}")
                continue
            problems.append(_describe(wrapped, {**error, "loc": ("rules", 0, *error["loc"])}))
        raise RulesetError(problems) from exc


def load_ruleset_text(text: str) -> RulesetFile:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesetError([f"Файл не читается как YAML: {exc}"]) from exc
    return parse_ruleset(raw)


def load_ruleset_file(path: Path) -> RulesetFile:
    return load_ruleset_text(path.read_text(encoding="utf-8"))


def dump_ruleset(ruleset: RulesetFile) -> str:
    """YAML для экспорта: порядок полей как в модели, без пустых значений, кириллица как есть."""
    data = ruleset.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    for rule in data["rules"]:
        for key in [k for k, v in rule.items() if v in ([], {}) and k != "legal_basis"]:
            del rule[key]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110)
