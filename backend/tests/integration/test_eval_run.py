"""Прогон оценки на сгенерированных пакетах: тот же граф задач, сверка с эталоном, отчёт."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from svetofor.core.config import LLMMode, get_settings
from svetofor.db.models import EvalRun, Package
from svetofor.eval.run import run_set
from svetofor.eval.synthetic import build_package, load_templates, write_package
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from svetofor.pipeline.ingest import IngestConfig
from svetofor.pipeline.orchestrator import AnalysisDeps
from svetofor.rules.loader import load_ruleset_file
from svetofor.rules.store import import_ruleset
from svetofor.rules.verify import QuoteVerifier
from svetofor.tasks import checks
from tests.support.fake_models import RoutingBackend, StemEmbedder, fact

pytestmark = pytest.mark.pg
RULES_FILE = Path(__file__).resolve().parents[3] / "rules" / "44fz_supplier_base.yaml"
FACTS = {
    "Проект контракта": [
        fact("payment_term_working_days", 15, "в течение 15 (пятнадцати) рабочих дней", "working"),
        fact("advance_percent", 0, "Авансирование не предусмотрено"),
    ]
}
VIOLATIONS = {
    "LIA-03": ("в размере 0,5 процента цены Контракта", "Пеня в процентах от цены выше законной."),
    "TRM-01": ("по любым основаниям", "Отказ возможен по любым основаниям."),
    "TERM-02": ("в течение 24 часов с момента её направления", "Сутки на заявку — мало."),
}


class HonestFake(RoutingBackend):
    """Знает ответ — отвечает с цитатой; не знает — «в порядке» (а не «не найдено»)."""

    def _verdict(self, code: str, prompt: str) -> dict[str, Any]:
        verdict = super()._verdict(code, prompt)
        return verdict if verdict["verdict"] == "violation" else {**verdict, "verdict": "ok"}


def test_run_set_end_to_end(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    rules = load_ruleset_file(RULES_FILE).rules
    documents, slots, catalog = load_templates()
    set_dir = tmp_path / "synthetic"
    for index, spec in enumerate(catalog["packages"], start=1):
        if spec["id"] in ("syn-001", "syn-003", "syn-007"):
            package = build_package(spec, index, documents, slots, catalog["subjects"])
            write_package(package, rules, set_dir / spec["id"], "pdf")
    with session_factory() as db:
        import_ruleset(db, load_ruleset_file(RULES_FILE))
        db.commit()
    gateway = LLMGateway(
        mode=LLMMode.LIVE,
        model_main="fake",
        model_fast="fake",
        backend=HonestFake(FACTS, VIOLATIONS),
        cache=MemoryCache(),
    )
    services = checks.Services(
        settings=get_settings(),
        session_factory=session_factory,
        ingest=IngestConfig(packages_dir=tmp_path / "packages"),
        analysis=AnalysisDeps(QuoteVerifier(), gateway, StemEmbedder()),
    )
    report = run_set(
        services, set_dir, tmp_path / "results", ruleset_code="44fz-supplier-base", model="fake"
    )

    assert report["packages"] == 3
    metrics = {name: data["value"] for name, data in report["metrics"].items()}
    # syn-001: 5 рисков (шаблон, 2 порога по фактам, 2 смысловых); syn-003: TERM-02 и
    # противоречия CON-01/CON-02; syn-007 чистый. Фейковая модель знает факты только контракта —
    # факты ТЗ и сроки оплаты в остальных пакетах находит канал грамматик (без него полнота 6/8)
    assert metrics["recall"] == 1.0
    assert metrics["precision"] == 1.0 and metrics["location_match"] == 1.0
    # поиск измеряется отдельно от модели: эталонный фрагмент был у модели перед глазами
    assert metrics["context_recall"] == 1.0
    assert metrics["reliability"] == 1.0 and metrics["facts_accuracy"] == 1.0
    # каждый канал по отдельности хуже, чем вместе: модель знает только оплату и аванс
    assert metrics["facts_accuracy_rules"] == 1.0
    assert metrics["facts_accuracy_model"] < metrics["facts_accuracy"]
    assert report["conflicts"]["total"] == 0
    assert report["per_rule"]["CON-01"]["tp"] == 1
    assert report["per_rule"]["CON-01"]["not_checked"] == 0
    assert report["metrics"]["recall"]["ci95"] is not None
    # расхождения с эталоном по пакетам — для разбора; здесь их нет
    assert all(d["missed"] == [] and d["extra"] == [] for d in report["details"])
    markdown = next((tmp_path / "results").glob("*_synthetic.md")).read_text(encoding="utf-8")
    assert "(3 пакета," in markdown and "| Полнота | 1,00 |" in markdown
    assert "| Карточка: только грамматики | 1,00 |" in markdown
    assert "| PAY-01 | 1 | 0 | 0 | 0 |" in markdown
    assert metrics["quotes_verified"] == 1.0
    with session_factory() as db:
        run = db.scalar(select(EvalRun))
        assert run is not None and run.dataset == "synthetic" and run.model_main == "fake"
        assert db.scalar(select(func.count()).select_from(Package)) == 0  # пакеты оценки удалены
