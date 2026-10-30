"""Набор «граница GREEN»: риск словами, которых система не знает.

Каждый случай из `eval/boundary/cases.yaml` — чистый пакет (извещение, контракт, ТЗ) с одной
вставкой. Здесь — сборка пакетов для `python -m svetofor.eval run --set boundary` (с настоящей
моделью) и детерминированные замеры, которым модель не нужна: какие механизмы системы вообще
«видят» рискованный текст.

Механизмы:
- `red_flag` — тревожные формулировки правила (`red_flags`);
- `hints` — общие с подсказками поиска правила основы слов (лексический поиск их бы поднял);
- `grammar` — грамматики карточки извлекли значение из вставки;
- `unparsed` — прототип «все числа в предложениях о поле разобраны»: срок или доля в предложении
  о сроке оплаты/приёмки/поставки/обеспечении, которых не покрыло ни одно выражение грамматик;
- `items` — прототип поштучной проверки: из скольких пунктов состоит вставка и самодостаточен ли
  хотя бы один (или становится им после подстановки текста пункта, на который ссылается).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from svetofor.db.enums import DocType
from svetofor.eval.synthetic import Block, write_docx, write_pdf
from svetofor.rules import fact_rules
from svetofor.rules.facts import SourceChunk
from svetofor.rules.model import LlmCheckRule, Rule
from svetofor.rules.rule_regex import TimeBudget, compile_rule_pattern
from svetofor.rules.verify import normalize

# в репозитории — eval/ в корне; в Docker-образе код в /app, наборы — томом /app/eval
_REPO_EVAL = Path(__file__).resolve().parents[3] / "eval"
CASES_FILE = (_REPO_EVAL if _REPO_EVAL.is_dir() else Path("/app/eval")) / "boundary" / "cases.yaml"

BASE_CONTRACT = [
    ("title", "ПРОЕКТ КОНТРАКТА"),
    ("paragraph", "Контракт заключается в соответствии с Федеральным законом № 44-ФЗ."),
    ("heading", "1. ПРЕДМЕТ КОНТРАКТА"),
    ("clause", "1.1. Поставщик обязуется поставить бумагу офисную в количестве 500 пачек."),
    ("heading", "2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ"),
    ("clause", "2.1. Заказчик выплачивает аванс в размере 30 % цены контракта."),
    (
        "clause",
        "2.2. Оплата производится в течение 7 рабочих дней с даты подписания документа о приемке.",
    ),
    ("heading", "3. СРОКИ ПОСТАВКИ"),
    ("clause", "3.1. Срок поставки — в течение 15 рабочих дней с даты заключения контракта."),
    ("heading", "4. ПРИЕМКА"),
    ("clause", "4.1. Приемка товара осуществляется в течение 5 рабочих дней с даты поставки."),
    ("heading", "5. ОБЕСПЕЧЕНИЕ"),
    (
        "clause",
        "5.1. Обеспечение исполнения контракта устанавливается в размере 5 % цены контракта.",
    ),
    ("heading", "6. ОТВЕТСТВЕННОСТЬ СТОРОН"),
    (
        "clause",
        "6.1. Пеня начисляется в размере одной трехсотой ключевой ставки Центрального банка.",
    ),
    ("clause", "6.2. Заказчик несет ответственность за просрочку оплаты в виде пени."),
    (
        "clause",
        "6.3. Штрафы начисляются в соответствии с Правилами, утвержденными постановлением "
        "Правительства Российской Федерации № 1042.",
    ),
    ("heading", "7. РАСТОРЖЕНИЕ КОНТРАКТА"),
    (
        "clause",
        "7.1. Расторжение контракта допускается по соглашению сторон, по решению суда или в связи "
        "с односторонним отказом стороны контракта в соответствии с гражданским законодательством.",
    ),
    (
        "clause",
        "7.2. Решение заказчика об одностороннем отказе вступает в силу через 10 дней с даты "
        "надлежащего уведомления поставщика.",
    ),
    ("heading", "8. ПРОЧИЕ УСЛОВИЯ"),
]
BASE_SPEC = [
    ("title", "ОПИСАНИЕ ОБЪЕКТА ЗАКУПКИ"),
    ("heading", "1. Требования к товару"),
    ("clause", "1.1. Бумага офисная формата А4, плотность 80 г/м2."),
    ("clause", "1.2. Количество: 500 пачек."),
    ("heading", "2. Требования к поставке"),
    ("clause", "2.1. Срок поставки: в течение 15 рабочих дней с даты заключения контракта."),
]
BASE_NOTICE = [
    ("title", "ИЗВЕЩЕНИЕ О ПРОВЕДЕНИИ ЭЛЕКТРОННОГО АУКЦИОНА"),
    ("heading", "1. Общие сведения"),
    ("clause", "1.1. Закупка осуществляется в соответствии с Федеральным законом № 44-ФЗ."),
    ("clause", "1.2. Размер обеспечения исполнения контракта — 5 % цены контракта."),
    ("clause", "1.3. Дополнительные требования к участникам закупки не установлены."),
]
# «в порядке» с цитатой нормального пункта — то, что ответила бы модель, не заметившая риск
NORMAL_QUOTES = {
    "LIA-01": "Штрафы начисляются в соответствии с Правилами, утвержденными постановлением "
    "Правительства Российской Федерации № 1042",
    "LIA-02": "Заказчик несет ответственность за просрочку оплаты в виде пени",
    "LIA-03": "Пеня начисляется в размере одной трехсотой ключевой ставки",
    "TRM-01": "Расторжение контракта допускается по соглашению сторон, по решению суда или в "
    "связи с односторонним отказом стороны контракта в соответствии с гражданским "
    "законодательством",
    "TRM-02": "Решение заказчика об одностороннем отказе вступает в силу через 10 дней с даты "
    "надлежащего уведомления поставщика",
}


@dataclass
class Case:
    id: str
    rule: str
    technique: str
    present: bool = True
    self_contained: bool = True
    section8: list[str] = field(default_factory=list)
    table: list[str] = field(default_factory=list)
    appendix: list[str] = field(default_factory=list)
    replace: dict[str, str] = field(default_factory=dict)

    @property
    def inserted(self) -> list[str]:
        """Вставленный текст: пункты раздела 8, строки таблицы, приложение, заменённые пункты."""
        return [*self.section8, *self.table, *self.appendix, *self.replace.values()]


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Case(**{**item, "id": str(item["id"])}) for item in data["cases"]]


# ─────────────────────────── сборка пакетов ───────────────────────────


def _blocks(rows: Sequence[tuple[str, str]]) -> list[Block]:
    return [Block(kind, text) for kind, text in rows]


def contract_rows(case: Case) -> list[tuple[str, str]]:
    rows = []
    for kind, text in BASE_CONTRACT:
        number = text.split(" ", 1)[0].rstrip(".") if kind == "clause" else ""
        if number in case.replace:
            text = f"{number}. {case.replace[number]}"
        rows.append((kind, text))
    clause = 1
    for text in case.section8:
        rows.append(("clause", f"8.{clause}. {text}"))
        clause += 1
    rows += [("clause", row) for row in case.table]
    return rows


def package_documents(case: Case) -> dict[str, list[Block]]:
    documents = {
        "Извещение.docx": _blocks(BASE_NOTICE),
        "Проект контракта.docx": _blocks(contract_rows(case)),
        "Описание объекта закупки.docx": _blocks(BASE_SPEC),
    }
    if case.appendix:
        rows = [("title", "Приложение № 3 к контракту"), ("heading", "Порядок взаимодействия")]
        rows += [("clause", f"{i}. {text}") for i, text in enumerate(case.appendix, start=1)]
        documents["Приложение 3.docx"] = _blocks(rows)
    return documents


def write_set(out_dir: Path, fmt: str = "docx", cases: Sequence[Case] | None = None) -> list[Path]:
    """Пакеты набора в `out_dir/<id>/` с `expected.yaml` для `svetofor.eval run`."""
    writer = write_docx if fmt == "docx" else write_pdf
    folders = []
    for case in cases or load_cases():
        folder = out_dir / case.id
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*"):
            old.unlink()
        for name, blocks in package_documents(case).items():
            writer(blocks, folder / re.sub(r"\.docx$", f".{fmt}", name))
        item: dict[str, Any] = {"rule": case.rule, "present": case.present}
        if case.present:
            item["quote_contains"] = case.inserted[-1][:40]
        expected = {
            "package": case.id,
            "description": f"{case.rule}: {case.technique}",
            "law": "44-ФЗ",
            "annotators": ["набор «граница GREEN» (эталон по построению)"],
            "expected": [item],
        }
        (folder / "expected.yaml").write_text(
            yaml.safe_dump(expected, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        folders.append(folder)
    return folders


# ─────────────────────────── детерминированные механизмы ───────────────────────────


def _chunk(text: str) -> SourceChunk:
    return SourceChunk("c", "d", "Проект контракта", 1, 1, text, 0, len(text) // 4)


def red_flag_hit(case: Case, rules: dict[str, Rule]) -> bool:
    rule = rules[case.rule]
    if not isinstance(rule, LlmCheckRule):
        return False
    text = " ".join(case.inserted)
    flags = [compile_rule_pattern(p) for p in rule.red_flags]
    return any(
        flag.search(t, TimeBudget()) is not None for flag in flags for t in (text, normalize(text))
    )


_WORD = re.compile(r"[а-яa-z]{4,}")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(normalize(text))}


def hint_overlap(case: Case, rules: dict[str, Rule]) -> set[str]:
    """Основы слов подсказок поиска правила, которые есть во вставке (лексический поиск)."""
    rule = rules[case.rule]
    hints = getattr(rule, "search_hints", [])
    return _stems(" ".join(hints)) & _stems(" ".join(case.inserted))


_FACT_OF_RULE = {
    "PAY-01": "payment_term_working_days",
    "ACC-01": "acceptance_term_working_days",
    "TERM-01": "delivery_term_days",
    "SEC-01": "contract_security_percent",
}


def grammar_values(case: Case) -> list[float | bool | str]:
    """Значения поля правила, которые грамматика извлекла из вставки (главное и кандидаты)."""
    key = _FACT_OF_RULE.get(case.rule)
    if key is None:
        return []
    chunks = [_chunk(t) for t in case.inserted]
    found = fact_rules.extract_rule_facts("d", DocType.CONTRACT, chunks).facts.get(key)
    if found is None:
        return []
    return [found.value, *(c["value"] for c in found.candidates)]


def unparsed_values(case_text: str) -> dict[str, list[str]]:
    """Неразобранные сроки и доли по полям — продуктовая проверка `fact_rules.unparsed_terms`."""
    chunks = [_chunk(case_text)]
    result: dict[str, list[str]] = {}
    for key in _FACT_OF_RULE.values():
        raws = [term.raw.lower() for term in fact_rules.unparsed_terms(key, chunks)]
        if raws:
            result[key] = raws
    return result


_REFERENCE = re.compile(r"пункт\w*\s+(\d+(?:\.\d+)+)")


def items(case: Case) -> list[str]:
    """Пункты вставки для поштучной проверки; ссылка «в пункте N» — с текстом пункта N."""
    numbered = {f"8.{i}": text for i, text in enumerate(case.section8, start=1)}
    result = []
    for text in case.inserted:
        refs = [numbered[n] for n in _REFERENCE.findall(text) if n in numbered]
        result.append(" ".join([text, *refs]))
    return result


def references_resolve(case: Case) -> bool:
    """Несамодостаточный риск собирается в один пункт подстановкой текста по ссылке."""
    return (
        any(_REFERENCE.search(text) for text in case.inserted)
        and len(items(case)) > 0
        and any(
            len(item) > len(text) for item, text in zip(items(case), case.inserted, strict=True)
        )
    )


def measure(case: Case, rules: dict[str, Rule]) -> dict[str, Any]:
    key = _FACT_OF_RULE.get(case.rule)
    unparsed = unparsed_values(" ".join(case.inserted)) if key else {}
    return {
        "id": case.id,
        "rule": case.rule,
        "technique": case.technique,
        "present": case.present,
        "red_flag": red_flag_hit(case, rules),
        "hints": sorted(hint_overlap(case, rules)),
        "grammar": grammar_values(case),
        "unparsed": unparsed.get(key, []) if key else [],
        "self_contained": case.self_contained,
        "references_resolve": references_resolve(case),
    }


def measure_all(rules: Sequence[Rule]) -> Iterator[dict[str, Any]]:
    by_id = {r.id: r for r in rules}
    for case in load_cases():
        yield measure(case, by_id)
