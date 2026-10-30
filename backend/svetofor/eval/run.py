"""Прогон оценки на размеченном наборе.

Каждый пакет проходит ровно тот же граф задач, что и настоящая проверка (Celery в режиме
«сразу в процессе»), затем результат сверяется с `expected.yaml`. Итог — JSON, Markdown
для README и запись `eval_runs` для страницы «Качество». Пакеты оценки после подсчёта удаляются.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.db.enums import CheckStatus, FindingStatus, RuleOutcome
from svetofor.db.models import Check, Chunk, EvalRun, Finding, Package, RuleResult
from svetofor.eval.bootstrap import bootstrap_ci
from svetofor.eval.metrics import (
    METRICS,
    FactResult,
    PackageResult,
    PairResult,
    channel_conflicts,
    per_rule,
    timing,
)
from svetofor.eval.synthetic import package_zip
from svetofor.pipeline.ingest import create_check
from svetofor.pipeline.orchestrator import check_rules, load_context
from svetofor.pipeline.storage import PackagePaths
from svetofor.rules.facts import DocumentFacts, merge_card
from svetofor.rules.facts_catalog import FACTS, FactKind
from svetofor.rules.model import LlmCheckRule
from svetofor.rules.store import latest_ruleset_version
from svetofor.rules.verify import normalize
from svetofor.tasks import checks
from svetofor.tasks.celery_app import celery_app

METRIC_TITLES = {
    "recall": "Полнота",
    "precision": "Точность",
    "location_match": "Совпадение места",
    "context_recall": "Нужный фрагмент в контексте модели",
    "facts_accuracy": "Точность карточки",
    "facts_accuracy_rules": "Карточка: только грамматики",
    "facts_accuracy_model": "Карточка: только модель",
    "quotes_verified": "Цитаты модели подтверждены",
    "reliability": "Пакеты без сбоев",
}


@contextmanager
def inline_tasks(services: checks.Services) -> Iterator[None]:
    """Задачи выполняются сразу в этом процессе — без брокера и воркеров."""
    saved = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = True
    checks.configure(services)
    try:
        yield
    finally:
        checks.configure(None)
        celery_app.conf.task_always_eager = saved


def _found_quotes(findings: list[Finding]) -> list[str]:
    quotes = [f.quote for f in findings if f.quote]
    for finding in findings:
        quotes.extend(e["quote"] for e in (finding.related or {}).get("evidence", []))
    return quotes


def channel_view(doc: DocumentFacts, channel: str) -> DocumentFacts:
    """Факты документа, какими их дал бы один канал, — чтобы сравнить каналы на эталоне."""
    facts = {}
    for key, fact in doc.facts.items():
        if fact.source in (channel, "both"):
            facts[key] = fact
        elif channel == "rules" and fact.conflict:  # победила модель, грамматика — в conflict
            facts[key] = replace(fact, value=fact.conflict["value"], source="rules")
    return replace(doc, facts=facts)


def _chunk_texts(db: Session, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    wanted = {uuid.UUID(i) for i in ids}
    return {str(c.id): c.text for c in db.scalars(select(Chunk).where(Chunk.id.in_(wanted)))}


def collect(
    services: checks.Services, check_id: uuid.UUID, data: dict[str, Any], seconds: float
) -> PackageResult:
    with services.session_factory() as db:
        check = db.get(Check, check_id)
        assert check is not None
        rule_results = list(db.scalars(select(RuleResult).where(RuleResult.check_id == check_id)))
        outcomes = {r.rule_code: r.outcome for r in rule_results}
        contexts = {
            r.rule_code: list((r.details or {})["context"])
            for r in rule_results
            if "context" in (r.details or {})
        }
        context_texts = _chunk_texts(db, [i for ids in contexts.values() for i in ids])
        findings = list(db.scalars(select(Finding).where(Finding.check_id == check_id)))
        llm_rules = {r.id for _, r in check_rules(db, check) if isinstance(r, LlmCheckRule)}
        documents = load_context(db, check).facts
        card = merge_card(documents)
        by_rules = merge_card([channel_view(d, "rules") for d in documents])
        by_model = merge_card([channel_view(d, "llm") for d in documents])
        failed = check.status is CheckStatus.FAILED or bool(check.counters.get("documents_failed"))
    shown = [f for f in findings if f.status is FindingStatus.SHOWN]
    result = PackageResult(data["package"], seconds=seconds, failed=failed)
    for item in data["expected"]:
        outcome = outcomes.get(item["rule"])
        predicted = outcome in (RuleOutcome.VIOLATION, RuleOutcome.ABSENT)
        location = None
        if item["present"] and predicted and item.get("quote_contains"):
            wanted = normalize(item["quote_contains"])
            quotes = _found_quotes([f for f in shown if f.rule_code == item["rule"]])
            location = any(wanted in normalize(q) for q in quotes)
        context_ok = None
        if item["present"] and item.get("quote_contains") and item["rule"] in contexts:
            seen = " ".join(context_texts.get(i, "") for i in contexts[item["rule"]])
            context_ok = normalize(item["quote_contains"]) in normalize(seen)
        result.pairs.append(
            PairResult(
                item["rule"],
                item["present"],
                predicted,
                outcome in (None, RuleOutcome.NOT_CHECKED),
                location,
                context_ok,
            )
        )
    for key, value in (data.get("facts") or {}).items():
        spec = FACTS.get(key)
        if spec is None or spec.kind is FactKind.TEXT:
            continue  # текстовые значения сравниваются глазами, не автоматически
        actual, rules_fact, model_fact = card.get(key), by_rules.get(key), by_model.get(key)
        result.facts.append(
            FactResult(
                key,
                value,
                actual.value if actual else None,
                rules_fact.value if rules_fact else None,
                model_fact.value if model_fact else None,
                bool(actual and actual.conflict),
            )
        )
    result.quotes_shown = sum(1 for f in shown if f.rule_code in llm_rules)
    result.quotes_rejected = sum(
        1 for f in findings if f.status is FindingStatus.REJECTED_UNVERIFIED
    )
    return result


def evaluate_package(
    services: checks.Services, folder: Path, ruleset_version_id: uuid.UUID, keep: bool = False
) -> PackageResult:
    data = yaml.safe_load((folder / "expected.yaml").read_text(encoding="utf-8"))
    services.ingest.packages_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=services.ingest.packages_dir) as tmp:
        upload = package_zip(folder, Path(tmp) / f"{folder.name}.zip")
        with services.session_factory() as db:
            check = create_check(
                db,
                upload=upload,
                original_name=f"{folder.name}.zip",
                config=services.ingest,
                ruleset_version_id=ruleset_version_id,
            )
            db.commit()
    started = time.perf_counter()
    checks.run_check.delay(str(check.id))
    result = collect(services, check.id, data, round(time.perf_counter() - started, 2))
    if not keep:
        with services.session_factory() as db:
            package = db.get(Package, check.package_id)
            if package is not None:
                PackagePaths(Path(package.storage_path)).remove()
                db.delete(package)
                db.commit()
    return result


def summarize(results: list[PackageResult]) -> dict[str, Any]:
    metrics = {}
    for name, metric in METRICS.items():
        value = metric(results)
        metrics[name] = {
            "value": round(value, 3) if value is not None else None,
            "ci95": bootstrap_ci(results, metric),
        }
    return {
        "metrics": metrics,
        "timing": timing(results),
        "per_rule": per_rule(results),
        "conflicts": channel_conflicts(results),
    }


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """3 → «3 пакета», 5 → «5 пакетов», 21 → «21 пакет»."""
    if n % 10 == 1 and n % 100 != 11:
        form = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{n} {form}"


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}".replace(".", ",")


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"## Качество: набор «{report['dataset']}» "
        f"({plural(report['packages'], ('пакет', 'пакета', 'пакетов'))}, "
        f"{report['created_at'][:10]})",
        "",
        f"Модель: {report['model'] or 'не подключена'}. "
        "Интервалы — бутстрэп по пакетам, 1000 повторов, 95 %.",
        "",
        "| Метрика | Значение | 95 % интервал |",
        "|---|---|---|",
    ]
    for name, data in report["metrics"].items():
        ci = data["ci95"]
        interval = f"{_fmt(ci[0])}–{_fmt(ci[1])}" if ci else "—"
        lines.append(f"| {METRIC_TITLES[name]} | {_fmt(data['value'])} | {interval} |")
    conflicts = report.get("conflicts") or {}
    if conflicts.get("total"):
        lines += [
            "",
            f"Каналы карточки разошлись: {conflicts['total']}; права модель — "
            f"{conflicts['model_right']}, грамматики — {conflicts['rules_right']}.",
        ]
    t = report["timing"]
    lines += [
        "",
        f"Время на пакет: медиана {_fmt(t['p50'])} с, 95-й перцентиль {_fmt(t['p95'])} с.",
        "",
        "| Правило | Найдено верно | Ложные | Пропущено | Не проверено |",
        "|---|---|---|---|---|",
    ]
    for rule, row in report["per_rule"].items():
        lines.append(f"| {rule} | {row['tp']} | {row['fp']} | {row['fn']} | {row['not_checked']} |")
    return "\n".join(lines) + "\n"


def run_set(
    services: checks.Services,
    set_dir: Path,
    results_dir: Path,
    *,
    ruleset_code: str,
    model: str | None,
    limit: int | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    folders = sorted(p for p in set_dir.iterdir() if (p / "expected.yaml").is_file())[:limit]
    if not folders:
        raise FileNotFoundError(f"В {set_dir} нет пакетов с expected.yaml")
    with services.session_factory() as db:
        version = latest_ruleset_version(db, ruleset_code)
        if version is None:
            raise LookupError(f"Набор правил «{ruleset_code}» не загружен")
        version_id = version.id
    with inline_tasks(services):
        results = [evaluate_package(services, folder, version_id, keep) for folder in folders]
    created = datetime.now(UTC).isoformat(timespec="seconds")
    report = {
        "dataset": set_dir.name,
        "created_at": created,
        "model": model,
        "green_sweep": services.analysis.green_sweep,
        "packages": len(results),
        **summarize(results),
        "details": [
            {
                "package": r.package,
                "seconds": r.seconds,
                "failed": r.failed,
                # расхождения с эталоном — с чего начинать разбор и проверку разметки
                "missed": [p.rule for p in r.pairs if p.expected and not p.predicted],
                "extra": [p.rule for p in r.pairs if p.predicted and not p.expected],
            }
            for r in results
        ],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    # с поштучной проверкой — отдельный отчёт: разница с обычным и есть её измеренная польза
    stem = f"{created[:10]}_{set_dir.name}" + ("_sweep" if services.analysis.green_sweep else "")
    (results_dir / f"{stem}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = markdown(report)
    (results_dir / f"{stem}.md").write_text(text, encoding="utf-8")
    with services.session_factory() as db:
        db.add(
            EvalRun(
                dataset=set_dir.name,
                model_main=model,
                ruleset_version_id=version_id,
                metrics={k: v for k, v in report.items() if k != "details"},
                report_md=text,
            )
        )
        db.commit()
    return report
