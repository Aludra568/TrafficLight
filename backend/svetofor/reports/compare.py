"""Сравнение редакций документации: что изменилось в пунктах, в карточке и в рисках.

Заказчик публикует изменения в извещение и документацию — поставщику нужно быстро понять,
что поменялось и стало ли рискованнее. Сравниваются две проверки (старая и новая редакция):
- документы — в пары по типу (и по имени, если документов одного типа несколько);
- пункты — сначала по номеру, затем по тексту: изменён, перенумерован, добавлен, удалён;
  у изменённого — какие слова ушли и какие пришли; документ без нумерации — по абзацам;
- карточка закупки — какие условия поменялись (срок оплаты, обеспечение…);
- риски — появились, ушли, сменили цвет.
Это разметка изменений текста, а не юридический разбор: смысл оценивают правила и специалист.
"""

from __future__ import annotations

import difflib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.db.enums import DOC_TYPE_TITLES, CheckStatus, DocumentStatus, FindingStatus
from svetofor.db.models import Check, Document, Fact, Finding, Package, Page, Section
from svetofor.pipeline.orchestrator import load_context
from svetofor.rules.facts import merge_card
from svetofor.rules.facts_catalog import FACTS, display_value
from svetofor.rules.verify import normalize

_NUMBER_PREFIX = re.compile(r"^\s*\d{1,2}(?:\.\d{1,3}){0,4}\.?\s*")
_MAX_DIFF_WORDS = 400
_FINISHED = (CheckStatus.DONE, CheckStatus.DONE_WITH_WARNINGS)


@dataclass(frozen=True)
class Unit:
    """Пункт (или абзац документа без нумерации) — единица сравнения."""

    key: str | None  # номер пункта; None — абзац без номера
    path: str
    text: str
    page: int

    @property
    def body(self) -> str:
        """Текст без номера пункта, нормализованный: перенумерация — не изменение."""
        return normalize(_NUMBER_PREFIX.sub("", self.text))


@dataclass
class UnitChange:
    status: str  # changed | renumbered | added | removed
    old: Unit | None
    new: Unit | None
    diff: list[dict[str, str]] = field(default_factory=list)  # [{op, text}] для «изменён»

    def as_dict(self) -> dict[str, Any]:
        def unit(u: Unit | None) -> dict[str, Any] | None:
            return (
                None
                if u is None
                else {"number": u.key, "path": u.path, "page": u.page, "text": u.text}
            )

        return {
            "status": self.status,
            "old": unit(self.old),
            "new": unit(self.new),
            "diff": self.diff,
        }


def word_diff(old: str, new: str) -> list[dict[str, str]]:
    """Изменения по словам: [{op: equal|delete|insert, text}] — соседние куски склеены."""
    a, b = old.split(), new.split()
    if len(a) + len(b) > 2 * _MAX_DIFF_WORDS:  # огромный пункт — без пословной разметки
        return [{"op": "delete", "text": old}, {"op": "insert", "text": new}]
    result: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            result.append({"op": "delete", "text": " ".join(a[i1:i2])})
        if tag in ("insert", "replace"):
            result.append({"op": "insert", "text": " ".join(b[j1:j2])})
        if tag == "equal":
            result.append({"op": "equal", "text": " ".join(a[i1:i2])})
    return result


def compare_units(old: Sequence[Unit], new: Sequence[Unit]) -> list[UnitChange]:
    """Пункты: по номеру, затем по тексту (перенумерованные). Без номеров — по порядку абзацев."""
    if not any(u.key for u in old) or not any(u.key for u in new):
        return _compare_sequences(old, new)
    old_by, new_by = {u.key: u for u in old if u.key}, {u.key: u for u in new if u.key}
    changes: list[UnitChange] = []
    for key, before in old_by.items():
        after = new_by.get(key)
        if after is not None and before.body != after.body:
            changes.append(UnitChange("changed", before, after, word_diff(before.text, after.text)))
    gone = [u for k, u in old_by.items() if k not in new_by]
    came = [u for k, u in new_by.items() if k not in old_by]
    came_by_body = {u.body: u for u in came}
    for before in gone:
        after = came_by_body.pop(before.body, None)
        if after is not None:
            changes.append(UnitChange("renumbered", before, after))
        else:
            changes.append(UnitChange("removed", before, None))
    matched = {id(u) for c in changes if c.status == "renumbered" for u in [c.new]}
    changes.extend(UnitChange("added", None, u) for u in came if id(u) not in matched)
    return changes


def _compare_sequences(old: Sequence[Unit], new: Sequence[Unit]) -> list[UnitChange]:
    a, b = [u.body for u in old], [u.body for u in new]
    changes: list[UnitChange] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        pairs = list(zip(old[i1:i2], new[j1:j2], strict=False)) if tag == "replace" else []
        for before, after in pairs:
            changes.append(UnitChange("changed", before, after, word_diff(before.text, after.text)))
        changes.extend(UnitChange("removed", u, None) for u in old[i1 + len(pairs) : i2])
        changes.extend(UnitChange("added", None, u) for u in new[j1 + len(pairs) : j2])
    return changes


# ─────────────────────────── из БД ───────────────────────────


def document_text(db: Session, document_id: uuid.UUID) -> str:
    """Текст документа по страницам и их смещениям — те же смещения, что у разделов."""
    pages = list(db.scalars(select(Page).where(Page.document_id == document_id)))
    size = max((p.char_offset_start + len(p.text) for p in pages), default=0)
    buffer = ["\n"] * size
    for page in pages:
        buffer[page.char_offset_start : page.char_offset_start + len(page.text)] = list(page.text)
    return "".join(buffer)


def document_units(db: Session, document: Document) -> list[Unit]:
    """Пункты документа: собственный текст раздела (без вложенных); без нумерации — абзацы."""
    text = document_text(db, document.id)
    sections = list(
        db.scalars(
            select(Section).where(Section.document_id == document.id).order_by(Section.order_index)
        )
    )
    numbered = [s for s in sections if s.number]
    if not numbered:
        return _paragraph_units(text, document.filename)
    first_child: dict[uuid.UUID, int] = {}
    for s in sections:
        if s.parent_id is not None:
            first_child[s.parent_id] = min(first_child.get(s.parent_id, s.char_start), s.char_start)
    return [
        Unit(
            s.number,
            s.path,
            " ".join(text[s.char_start : first_child.get(s.id, s.char_end)].split()),
            s.page_from,
        )
        for s in numbered
    ]


def _paragraph_units(text: str, label: str) -> list[Unit]:
    units = []
    for block in re.split(r"\n\s*\n|\n", text):
        clean = " ".join(block.split())
        if len(clean) >= 3:
            units.append(Unit(None, label, clean, 1))
    return units


def _ready(db: Session, check: Check) -> list[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.package_id == check.package_id, Document.status == DocumentStatus.READY)
            .order_by(Document.relative_path)
        )
    )


def pair_documents(
    old: list[Document], new: list[Document]
) -> list[tuple[Document | None, Document | None]]:
    """Пары документов: по типу; несколько одного типа — по похожести имени файла."""
    pairs: list[tuple[Document | None, Document | None]] = []
    left = list(old)
    for after in new:
        same_type = [d for d in left if d.doc_type is after.doc_type]
        if not same_type:
            pairs.append((None, after))
            continue
        before = max(
            same_type,
            key=lambda d: difflib.SequenceMatcher(a=d.filename, b=after.filename).ratio(),
        )
        left.remove(before)
        pairs.append((before, after))
    pairs.extend((before, None) for before in left)
    return pairs


def _card_changes(db: Session, old: Check, new: Check) -> list[dict[str, Any]]:
    before = merge_card(load_context(db, old).facts)
    after = merge_card(load_context(db, new).facts)
    changes = []
    for key, spec in FACTS.items():
        a, b = before.get(key), after.get(key)
        old_value = display_value(key, a.value) if a else None
        new_value = display_value(key, b.value) if b else None
        if old_value != new_value or (a and b and a.unit != b.unit):
            changes.append(
                {
                    "key": key,
                    "label": spec.label,
                    "old": old_value,
                    "new": new_value,
                    "old_quote": a.quote if a else None,
                    "new_quote": b.quote if b else None,
                }
            )
    return changes


def _risk_changes(db: Session, old: Check, new: Check) -> dict[str, list[dict[str, Any]]]:
    def shown(check: Check) -> dict[str, Finding]:
        rows = db.scalars(
            select(Finding).where(
                Finding.check_id == check.id, Finding.status == FindingStatus.SHOWN
            )
        )
        return {f.rule_code: f for f in rows}

    before, after = shown(old), shown(new)

    def item(f: Finding) -> dict[str, Any]:
        return {
            "rule_code": f.rule_code,
            "title": f.title,
            "severity": f.severity.value,
            "quote": f.quote,
        }

    return {
        "appeared": [item(f) for code, f in after.items() if code not in before],
        "resolved": [item(f) for code, f in before.items() if code not in after],
        "severity_changed": [
            {**item(after[code]), "old_severity": before[code].severity.value}
            for code in after
            if code in before and after[code].severity is not before[code].severity
        ],
    }


def compare_checks(db: Session, old: Check, new: Check) -> dict[str, Any]:
    """Что изменилось от редакции `old` к `new`: документы, карточка, риски."""
    documents = []
    for before, after in pair_documents(_ready(db, old), _ready(db, new)):
        changes = (
            compare_units(document_units(db, before), document_units(db, after))
            if before and after
            else []
        )
        main = after or before
        assert main is not None
        documents.append(
            {
                "doc_type": main.doc_type.value,
                "doc_type_title": DOC_TYPE_TITLES[main.doc_type],
                "old_filename": before.filename if before else None,
                "new_filename": after.filename if after else None,
                "status": "added"
                if before is None
                else "removed"
                if after is None
                else ("changed" if changes else "same"),
                "changes": [c.as_dict() for c in changes],
            }
        )
    return {
        "old_check_id": str(old.id),
        "new_check_id": str(new.id),
        "documents": documents,
        "card": _card_changes(db, old, new),
        "risks": _risk_changes(db, old, new),
    }


def previous_editions(
    db: Session, check: Check, owner_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Прежние проверки той же закупки (по номеру извещения в карточке) — что сравнивать.

    `owner_id` — только пакеты этого владельца (специалист видит свои, `api/access.py`).
    """
    number = db.scalar(
        select(Fact.value_text).where(Fact.check_id == check.id, Fact.key == "procurement_number")
    )
    if not number:
        return []
    rows = db.execute(
        select(Check, Package.title)
        .join(Fact, Fact.check_id == Check.id)
        .join(Package, Package.id == Check.package_id)
        .where(
            Fact.key == "procurement_number",
            Fact.value_text == number,
            Check.id != check.id,
            Check.created_at < check.created_at,
            Check.status.in_(_FINISHED),
            *([Package.owner_id == owner_id] if owner_id is not None else []),
        )
        .order_by(Check.created_at.desc())
    ).all()
    seen: set[uuid.UUID] = set()
    result = []
    for other, title in rows:
        if other.id in seen:
            continue
        seen.add(other.id)
        result.append(
            {
                "check_id": str(other.id),
                "package_title": title,
                "created_at": other.created_at.isoformat(),
                "light": other.light.value if other.light else None,
                "procurement_number": number,
            }
        )
    return result
