"""Синтетический набор: эталон корректен по построению и читается нашим же конвейером."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import docx
import pytest
import yaml

from svetofor.eval.synthetic import build_package, generate, load_templates, money, package_zip
from svetofor.pipeline.extract import extract_document
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.verify import normalize

RULES = load_ruleset_file(
    Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
).rules
DOCUMENTS, SLOTS, CATALOG = load_templates()
PACKAGES = [
    build_package(spec, i, DOCUMENTS, SLOTS, CATALOG["subjects"])
    for i, spec in enumerate(CATALOG["packages"], start=1)
]


def test_every_rule_is_planted_somewhere() -> None:
    planted = {t.rule for p in PACKAGES for t in p.triggers}
    assert planted == {r.id for r in RULES}


def test_clean_and_trap_packages_exist() -> None:
    clean = [p for p in PACKAGES if not p.triggers]
    traps = [p for p in PACKAGES if p.description.startswith("Ловушка") and not p.triggers]
    assert len(clean) >= 3 and len(traps) >= 2
    assert 10 <= len(PACKAGES) <= 20


@pytest.mark.parametrize("package", PACKAGES, ids=[p.id for p in PACKAGES])
def test_quotes_exist_and_numbering_is_continuous(package: object) -> None:
    from svetofor.eval.synthetic import GeneratedPackage, expected

    assert isinstance(package, GeneratedPackage)
    data = expected(package, RULES)
    assert [item["rule"] for item in data["expected"]] == [r.id for r in RULES]
    for item in data["expected"]:
        if "quote_contains" in item:
            texts = [
                b.text
                for name, blocks in package.documents.values()
                if name == item["document"]
                for b in blocks
            ]
            # как при оценке — по нормализованному тексту (латиница-двойник, ё, кавычки)
            assert any(normalize(item["quote_contains"]) in normalize(t) for t in texts)
    for _, blocks in package.documents.values():
        numbers = [b.text.split(" ", 1)[0] for b in blocks if b.kind == "clause"]
        by_section: dict[str, list[int]] = {}
        for number in numbers:
            section, clause = number.rstrip(".").split(".")
            by_section.setdefault(section, []).append(int(clause))
        assert all(v == list(range(1, len(v) + 1)) for v in by_section.values())


def test_facts_of_risky_package() -> None:
    package = PACKAGES[0]
    assert (
        package.facts["payment_term_working_days"] == 15 and package.facts["advance_percent"] == 0
    )
    assert package.facts["nmck_rub"] == 1250000 and package.facts["law"] == "44-ФЗ"
    mismatch = next(p for p in PACKAGES if p.id == "syn-003")
    assert mismatch.facts["delivery_term_days"] == 30  # в карточку идёт значение из ТЗ


def test_money() -> None:
    assert money(1250000) == "1 250 000,00" and money(780000.5) == "780 000,50"


def test_docx_written(tmp_path: Path) -> None:
    folders = generate(tmp_path, RULES, "docx")
    assert len(folders) == len(PACKAGES)
    first = folders[0]
    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(
        [
            "expected.yaml",
            "Извещение о проведении электронного аукциона.docx",
            "Описание объекта закупки.docx",
            "Проект контракта.docx",
        ]
    )
    contract = docx.Document(str(first / "Проект контракта.docx"))
    text = "\n".join(p.text for p in contract.paragraphs)
    assert "2.4. Оплата за поставленный Товар производится в течение 15 (пятнадцати)" in text
    archive = package_zip(first, tmp_path / "upload.zip")
    with zipfile.ZipFile(archive) as zf:
        assert "expected.yaml" not in zf.namelist() and len(zf.namelist()) == 3
    # разметка настоящих пакетов: спорные случаи и источник — не документы закупки
    (first / "notes.md").write_text("спорно", encoding="utf-8")
    (first / "source.txt").write_text("https://zakupki.gov.ru/…", encoding="utf-8")
    with zipfile.ZipFile(package_zip(first, tmp_path / "again.zip")) as zf:
        assert len(zf.namelist()) == 3


def test_pdf_readable_by_pipeline(tmp_path: Path) -> None:
    folder = generate(tmp_path, RULES, "pdf")[2]  # syn-003: противоречия
    data = yaml.safe_load((folder / "expected.yaml").read_text(encoding="utf-8"))
    for item in data["expected"]:
        if "quote_contains" not in item:
            continue
        assert item["document"].endswith(".pdf")
        text = extract_document(folder / item["document"]).text
        assert normalize(item["quote_contains"]) in normalize(text), item["rule"]
    contract = extract_document(folder / "Проект контракта.pdf").text
    assert re.search(r"3\.1\. Срок поставки Товара", contract)


def test_demo_package(tmp_path: Path) -> None:
    from svetofor.eval.synthetic import generate_demo

    target = generate_demo(tmp_path / "samples" / "demo.zip", RULES)
    with zipfile.ZipFile(target) as zf:
        assert sorted(zf.namelist()) == sorted(
            [
                "Извещение о проведении электронного аукциона.docx",
                "Описание объекта закупки.docx",
                "Проект контракта.docx",
            ]
        )
    data = yaml.safe_load(
        (tmp_path / "samples" / "demo-44fz-postavka" / "expected.yaml").read_text(encoding="utf-8")
    )
    present = {item["rule"] for item in data["expected"] if item["present"]}
    assert present == {"PAY-01", "PAY-02", "VOL-01", "LIA-03", "TRM-01", "SUB-01", "CON-01"}
    assert data["facts"]["procurement_number"] == "0373100012326000777"
