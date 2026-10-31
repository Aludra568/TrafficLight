"""Замер поиска без модели: попадает ли нужный фрагмент в контекст правила.

Тот же показатель, что `context_recall` полного замера, но за секунды и без токенов: пакеты
загружаются один раз (классификация документов моделью — только при первой загрузке, ответы
ложатся в кэш), дальше для каждой группы llm_check-правил повторяется поиск движка
(`search_request` → `HybridSearch.search` → `expand`). Для промаха — причина:

- `cut_by_budget` — фрагмент был в выдаче RRF, но не влез в бюджет токенов;
- `not_ranked` — фрагмент в документах нужного типа, но не в выдаче;
- `wrong_doc_type` — фраза только в документах типа, которого нет в `applies_to`;
- `not_in_chunks` — фразы нет ни в одном фрагменте пакета (нарезка, OCR, ошибка разметки).

Повторный поиск с половинным бюджетом (ответ модели «длиннее контекста») не воспроизводится.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from svetofor.db.models import Check, Chunk, Document, Package
from svetofor.eval.retrieval_report import RetrievalItem, RetrievalReport, save_report
from svetofor.eval.synthetic import package_zip
from svetofor.pipeline.ingest import (
    classify_with_llm,
    create_check,
    new_check,
    prepare_package,
    process_document,
    register_attempt,
)
from svetofor.pipeline.orchestrator import embed_chunks, load_context
from svetofor.rules.engine import inapplicable_reason
from svetofor.rules.facts import SourceChunk, fits_whole
from svetofor.rules.model import LlmCheckRule
from svetofor.rules.results import CheckContext
from svetofor.rules.store import latest_ruleset_version, ruleset_rules
from svetofor.rules.types import EngineDeps
from svetofor.rules.types.llm_check import batches, search_request
from svetofor.rules.verify import normalize
from svetofor.search.hybrid import PER_METHOD, QUOTA, HybridSearch, Query, SearchHit
from svetofor.tasks import checks

REASONS = ("cut_by_budget", "not_ranked", "wrong_doc_type", "not_in_chunks")


def _has(wanted: str, texts: Sequence[str]) -> bool:
    return any(wanted in normalize(t) for t in texts)


def miss_reason(
    quote: str,
    context: Sequence[str],
    hits: Sequence[str],
    right_type: Sequence[str],
    package: Sequence[str],
) -> str | None:
    """None — фраза в контексте модели; иначе первая подходящая причина из `REASONS`.

    Аргументы — тексты фрагментов: контекст после `expand`, выдача RRF до обрезки, все
    фрагменты документов нужного типа, все фрагменты пакета.
    """
    wanted = normalize(quote)
    if _has(wanted, context) or _has(wanted, [" ".join(context)]):
        return None
    for reason, texts in zip(REASONS, (hits, right_type, package), strict=False):
        if _has(wanted, texts):
            return reason
    return "not_in_chunks"


def best_ranks(
    hits: Sequence[SearchHit], chunks: Sequence[SourceChunk], quote: str
) -> dict[str, int]:
    """Места в списках поиска лучшего фрагмента с фразой; пусто — фрагмента нет в выдаче."""
    wanted = normalize(quote)
    with_quote = {c.id for c in chunks if wanted in normalize(c.text)}
    found = [h for h in hits if str(h.chunk_id) in with_quote and h.ranks]
    return min(found, key=lambda h: min(h.ranks.values())).ranks if found else {}


def _loaded_package(services: checks.Services, name: str) -> uuid.UUID | None:
    """Пакет, уже загруженный прошлым прогоном: фрагменты есть и у всех есть векторы."""
    with services.session_factory() as db:
        ids = db.scalars(
            select(Package.id)
            .where(Package.source_filename == f"{name}.zip")
            .order_by(Package.created_at.desc())
        )
        for package_id in ids:
            in_package = (
                select(func.count(Chunk.id))
                .join(Document, Document.id == Chunk.document_id)
                .where(Document.package_id == package_id)
            )
            total = db.scalar(in_package) or 0
            missing = db.scalar(in_package.where(Chunk.embedding.is_(None))) or 0
            if total and (not missing or services.analysis.embedder is None):
                return package_id
    return None


def load_package(services: checks.Services, folder: Path, version_id: uuid.UUID) -> uuid.UUID:
    """Распаковка, разбор, тип документов и векторы — как в проверке, без правил."""
    loaded = _loaded_package(services, folder.name)
    if loaded is not None:
        return loaded
    services.ingest.packages_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=services.ingest.packages_dir) as tmp:
        upload = package_zip(folder, Path(tmp) / f"{folder.name}.zip")
        with services.session_factory() as db:
            check = create_check(
                db,
                upload=upload,
                original_name=f"{folder.name}.zip",
                config=services.ingest,
                ruleset_version_id=version_id,
            )
            db.commit()
            check_id, package_id = check.id, check.package_id
    gateway = services.analysis.gateway
    classify = classify_with_llm(gateway) if gateway is not None else None
    for document_id in prepare_package(services.session_factory, check_id, services.ingest):
        if register_attempt(services.session_factory, document_id):
            process_document(services.session_factory, document_id, services.ingest, classify)
    if services.analysis.embedder is not None:
        embed_chunks(services.session_factory, check_id, services.analysis.embedder)
    return package_id


def _context(
    services: checks.Services, package_id: uuid.UUID, version_id: uuid.UUID
) -> CheckContext:
    with services.session_factory() as db:
        check = db.scalars(
            select(Check).where(Check.package_id == package_id).order_by(Check.created_at.desc())
        ).first()
        if check is None:
            package = db.get(Package, package_id)
            assert package is not None
            check = new_check(db, package, version_id)
            db.commit()
        return load_context(db, check)


def measure_package(
    services: checks.Services,
    folder: Path,
    package_id: uuid.UUID,
    version_id: uuid.UUID,
    rules: Sequence[LlmCheckRule],
    params: dict[str, Any],
) -> list[RetrievalItem]:
    data = yaml.safe_load((folder / "expected.yaml").read_text(encoding="utf-8"))
    wanted: dict[str, list[str]] = {}
    for item in data["expected"]:
        if item.get("present") and item.get("quote_contains"):
            wanted.setdefault(item["rule"], []).append(str(item["quote_contains"]))
    ctx = _context(services, package_id, version_id)
    package_texts = [c.text for d in ctx.documents for c in d.chunks]
    applicable = [r for r in rules if inapplicable_reason(r, ctx) is None]
    items: list[RetrievalItem] = []
    for group in batches(applicable, EngineDeps.max_rules_per_call):
        doc_types = list(dict.fromkeys(t for r in group for t in r.applies_to))
        candidates = [c for d in ctx.documents_of(doc_types) for c in d.chunks]
        hits: list[SearchHit] = []
        hit_texts: list[str] = []
        if candidates and fits_whole(candidates, params["whole_budget"]):
            mode, context = "whole", candidates
        else:
            mode = "retrieval"
            with services.session_factory() as db:
                search = HybridSearch(db, services.analysis.embedder)
                queries = [Query(r.text, r.hints) for r in map(search_request, group)]
                hits = search.search(
                    package_id=package_id,
                    doc_types=doc_types,
                    queries=queries,
                    limit=params["top"],
                    per_method=params["per_method"],
                    quota=params["quota"],
                ).hits
                expanded = search.expand(hits, params["budget"])
                context = [c for c in candidates if c.id in {str(e.id) for e in expanded}]
            hit_ids = {str(h.chunk_id) for h in hits}
            hit_texts = [c.text for c in candidates if c.id in hit_ids]
        context_texts = [c.text for c in context]
        right_type = [c.text for c in candidates]
        for rule in group:
            for quote in wanted.get(rule.id, []):
                reason = miss_reason(
                    quote,
                    context_texts,
                    hit_texts,
                    right_type,
                    package_texts,
                )
                ranks = best_ranks(hits, candidates, quote)
                items.append(
                    RetrievalItem(folder.name, rule.id, mode, reason is None, reason, quote, ranks)
                )
    for rule in rules:
        if rule not in applicable:
            for quote in wanted.get(rule.id, []):
                reason = miss_reason(quote, [], [], [], package_texts)
                items.append(
                    RetrievalItem(folder.name, rule.id, "inapplicable", False, reason, quote)
                )
    return items


def run_retrieval(
    services: checks.Services,
    set_dir: Path,
    results_dir: Path,
    *,
    ruleset_code: str,
    per_method: int = PER_METHOD,
    quota: int = QUOTA,
    top: int | None = None,
    budget: int | None = None,
    limit: int | None = None,
) -> RetrievalReport:
    folders = sorted(p for p in set_dir.iterdir() if (p / "expected.yaml").is_file())[:limit]
    if not folders:
        raise FileNotFoundError(f"В {set_dir} нет пакетов с expected.yaml")
    with services.session_factory() as db:
        version = latest_ruleset_version(db, ruleset_code)
        if version is None:
            raise LookupError(f"Набор правил «{ruleset_code}» не загружен")
        version_id = version.id
        rules = [
            r for _, r in ruleset_rules(db, version) if isinstance(r, LlmCheckRule) and r.enabled
        ]
    budgets = services.analysis.budgets
    params: dict[str, Any] = {
        "per_method": per_method,
        "quota": quota,
        "top": top,
        "budget": budget or budgets.retrieval,
        "whole_budget": budgets.whole,
    }
    items: list[RetrievalItem] = []
    for folder in folders:
        package_id = load_package(services, folder, version_id)
        items.extend(measure_package(services, folder, package_id, version_id, rules, params))
    report = RetrievalReport(
        dataset=set_dir.name,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        packages=len(folders),
        params=params,
        items=items,
    )
    save_report(report, results_dir)
    return report
