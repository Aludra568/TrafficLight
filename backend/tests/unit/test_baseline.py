"""Базовая линия «склеили текст — спросили модель» считается на том же эталоне."""

from __future__ import annotations

import json
from pathlib import Path

from svetofor.core.config import LLMMode
from svetofor.eval.baseline import baseline_markdown, run_baseline
from svetofor.eval.synthetic import build_package, load_templates, write_package
from svetofor.llm.backend import ChatReply, ChatRequest
from svetofor.llm.cache import MemoryCache
from svetofor.llm.gateway import LLMGateway
from tests.support.rulesets import STARTER_RULES


class NaiveModel:
    """Находит один настоящий риск с настоящей цитатой и «находит» второй с выдуманной."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, request: ChatRequest) -> ChatReply:
        self.prompts.append(request.messages[1].content)
        answer = {
            "risks": [
                {"rule_id": "VOL-01", "quote": "по мере необходимости Заказчика"},
                {"rule_id": "LIA-01", "quote": "штраф составляет 50 процентов цены контракта"},
            ]
        }
        return ChatReply(json.dumps(answer, ensure_ascii=False), 1000, 50, "stop")


def test_baseline_on_synthetic_package(tmp_path: Path) -> None:
    documents, slots, catalog = load_templates()
    spec = next(p for p in catalog["packages"] if p["id"] == "syn-001")
    package = build_package(spec, 1, documents, slots, catalog["subjects"])
    write_package(package, STARTER_RULES, tmp_path / "set" / "syn-001", "docx")
    model = NaiveModel()
    gateway = LLMGateway(
        mode=LLMMode.LIVE, model_main="m", model_fast="m", backend=model, cache=MemoryCache()
    )
    report = run_baseline(gateway, tmp_path / "set", STARTER_RULES, tmp_path / "results", model="m")
    assert "=== Проект контракта.docx ===" in model.prompts[0]  # текст файлов — в одном промпте
    metrics = {k: v["value"] for k, v in report["metrics"].items()}
    # VOL-01 заложен в syn-001, LIA-01 — нет: одно верное, одно ложное срабатывание
    assert metrics["precision"] == 0.5
    assert 0 < metrics["recall"] < 1
    assert metrics["quotes_found"] == 0.5  # вторая цитата выдумана — её нет в документах
    text = baseline_markdown(report)
    assert "| Цитаты найдены в документах | 0,50 |" in text
    assert next((tmp_path / "results").glob("*_baseline.md")).read_text(encoding="utf-8") == text
