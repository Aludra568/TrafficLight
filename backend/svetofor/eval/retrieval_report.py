"""Отчёт замера поиска без модели: JSON и Markdown в `eval/results/`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from svetofor.eval.run import plural

REASON_TITLES = {
    "cut_by_budget": "в выдаче, но не влез в бюджет",
    "not_ranked": "нет в выдаче поиска",
    "wrong_doc_type": "только в документе другого типа",
    "not_in_chunks": "нет ни в одном фрагменте пакета",
}


@dataclass(frozen=True)
class RetrievalItem:
    """Один ожидаемый риск: попал ли его фрагмент в контекст правила."""

    package: str
    rule: str
    mode: str  # retrieval — поиск; whole — документы целиком; inapplicable — правило не применялось
    found: bool
    reason: str | None
    quote: str
    ranks: dict[str, int] = field(default_factory=dict)  # место в списках поиска, с 1


@dataclass(frozen=True)
class RetrievalReport:
    dataset: str
    created_at: str
    packages: int
    params: dict[str, Any]
    items: list[RetrievalItem]

    @property
    def found(self) -> int:
        return sum(1 for i in self.items if i.found)

    @property
    def recall(self) -> float | None:
        return self.found / len(self.items) if self.items else None

    def reasons(self) -> dict[str, int]:
        counts = dict.fromkeys(REASON_TITLES, 0)
        for item in self.items:
            if item.reason:
                counts[item.reason] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "created_at": self.created_at,
            "packages": self.packages,
            "params": self.params,
            "total": len(self.items),
            "found": self.found,
            "recall": None if self.recall is None else round(self.recall, 3),
            "reasons": self.reasons(),
            "items": [asdict(i) for i in self.items],
        }


def _ranks(ranks: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(ranks.items())) or "—"


def markdown(report: RetrievalReport) -> str:
    p = report.params
    recall = "—" if report.recall is None else f"{report.recall:.3f}".replace(".", ",")
    lines = [
        f"## Замер поиска без модели: набор «{report.dataset}» "
        f"({plural(report.packages, ('пакет', 'пакета', 'пакетов'))}, {report.created_at[:10]})",
        "",
        f"Параметры: per_method {p['per_method']}, квота {p.get('quota', 0)}, "
        f"top {p['top'] or 'авто'}, "
        f"бюджет {p['budget']} токенов, «целиком» до {p['whole_budget']} токенов.",
        "",
        f"Нужный фрагмент в контексте правила: **{recall}** "
        f"({report.found} из {len(report.items)}).",
        "",
        "### Почему не попал",
        "",
    ]
    lines += [f"- {REASON_TITLES[r]} (`{r}`): {n}" for r, n in report.reasons().items()]
    misses = [i for i in report.items if not i.found]
    lines += ["", "### Промахи", ""]
    if not misses:
        lines.append("Промахов нет.")
    else:
        lines += [
            "| Пакет | Правило | Режим | Причина | Места в поиске | Фраза |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {i.package} | {i.rule} | {i.mode} | {i.reason} "
            f"| {_ranks(i.ranks)} | {i.quote[:60]} |"
            for i in misses
        ]
    return "\n".join(lines) + "\n"


def save_report(report: RetrievalReport, results_dir: Path) -> Path:
    """`<дата>_retrieval_<набор>.json` и `.md`; возвращает путь к Markdown."""
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = results_dir / f"{report.created_at[:10]}_retrieval_{report.dataset}"
    stem.with_suffix(".json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    path = stem.with_suffix(".md")
    path.write_text(markdown(report), encoding="utf-8")
    return path
