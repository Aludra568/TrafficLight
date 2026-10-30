"""Правила в БД: версии правил и наборов.

Правило в БД — неизменяемые версии с полным описанием (`body`). Проверка ссылается на версию
набора, а та — на версии правил: отчёт воспроизводим, даже если правила потом поменяли.
Импорт идемпотентен: без изменений новых версий не появляется.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from svetofor.db.models import Rule as RuleRow
from svetofor.db.models import Ruleset, RulesetVersion, RuleVersion
from svetofor.rules.model import Rule, RulesetFile, RulesetMeta

_RULE_ADAPTER: TypeAdapter[Rule] = TypeAdapter(Rule)


def rule_body(rule: Rule) -> dict[str, Any]:
    return rule.model_dump(mode="json")


def rule_from_body(body: dict[str, Any]) -> Rule:
    """Сохранённое правило: проверено при записи, замер времени выражений не повторяется."""
    return _RULE_ADAPTER.validate_python(body, context={"stored": True})


def _as_datetime(value: date | None) -> datetime | None:
    return datetime(value.year, value.month, value.day, tzinfo=UTC) if value else None


def save_rule(
    db: Session, rule: Rule, *, author_id: uuid.UUID | None = None, note: str | None = None
) -> RuleVersion:
    """Новая версия правила, если описание изменилось; иначе — текущая версия."""
    row = db.scalar(select(RuleRow).where(RuleRow.code == rule.id))
    if row is None:
        row = RuleRow(code=rule.id)
        db.add(row)
        db.flush()
    body = rule_body(rule)
    current = db.get(RuleVersion, row.current_version_id) if row.current_version_id else None
    if current is not None and current.body == body:
        if row.is_deleted:
            row.is_deleted = False
        return current
    last = db.scalar(select(func.max(RuleVersion.version)).where(RuleVersion.rule_id == row.id))
    version = RuleVersion(
        rule_id=row.id,
        version=(last or 0) + 1,
        body=body,
        status=rule.status,
        verified_by=rule.verified_by,
        verified_at=_as_datetime(rule.verified_at),
        author_id=author_id,
        change_note=note,
    )
    db.add(version)
    db.flush()
    row.current_version_id = version.id
    row.is_deleted = False
    return version


def latest_ruleset_version(db: Session, code: str) -> RulesetVersion | None:
    return db.scalar(
        select(RulesetVersion)
        .join(Ruleset, Ruleset.id == RulesetVersion.ruleset_id)
        .where(Ruleset.code == code)
        .order_by(RulesetVersion.version.desc())
        .limit(1)
    )


def publish_ruleset(
    db: Session,
    meta: RulesetMeta,
    rule_version_ids: Sequence[uuid.UUID],
    *,
    author_id: uuid.UUID | None = None,
) -> RulesetVersion:
    """Новая версия набора, если состав версий правил изменился; иначе — последняя."""
    ruleset = db.scalar(select(Ruleset).where(Ruleset.code == meta.code))
    if ruleset is None:
        ruleset = Ruleset(code=meta.code, title=meta.title, law=meta.law)
        db.add(ruleset)
        db.flush()
    else:
        ruleset.title, ruleset.law = meta.title, meta.law
    latest = latest_ruleset_version(db, meta.code)
    ids = list(rule_version_ids)
    if latest is not None and list(latest.rule_version_ids) == ids:
        return latest
    version = RulesetVersion(
        ruleset_id=ruleset.id,
        version=(latest.version if latest else 0) + 1,
        rule_version_ids=ids,
        author_id=author_id,
    )
    db.add(version)
    db.flush()
    return version


def publish_current(
    db: Session, code: str, *, author_id: uuid.UUID | None = None
) -> RulesetVersion | None:
    """Новая версия набора из текущих версий всех неудалённых правил.

    Порядок — как в последней версии набора, новые правила — в конце по коду.
    """
    ruleset = db.scalar(select(Ruleset).where(Ruleset.code == code))
    if ruleset is None:
        return None
    latest = latest_ruleset_version(db, code)
    previous_order: dict[uuid.UUID, int] = {}
    if latest is not None:
        versions = db.scalars(
            select(RuleVersion).where(RuleVersion.id.in_(latest.rule_version_ids))
        )
        position = {vid: i for i, vid in enumerate(latest.rule_version_ids)}
        previous_order = {v.rule_id: position[v.id] for v in versions}
    rows = list(db.scalars(select(RuleRow).where(RuleRow.is_deleted.is_(False))))
    rows.sort(key=lambda r: (previous_order.get(r.id, len(previous_order)), r.code))
    ids = [r.current_version_id for r in rows if r.current_version_id is not None]
    meta = RulesetMeta(code=ruleset.code, title=ruleset.title, law=ruleset.law)
    return publish_ruleset(db, meta, ids, author_id=author_id)


def import_ruleset(
    db: Session,
    ruleset: RulesetFile,
    *,
    author_id: uuid.UUID | None = None,
    note: str = "импорт из YAML",
) -> RulesetVersion:
    versions = [save_rule(db, rule, author_id=author_id, note=note) for rule in ruleset.rules]
    return publish_ruleset(db, ruleset.ruleset, [v.id for v in versions], author_id=author_id)


def ruleset_rules(db: Session, ruleset_version: RulesetVersion) -> list[tuple[uuid.UUID, Rule]]:
    """(id версии правила, правило) в порядке набора."""
    ids = list(ruleset_version.rule_version_ids)
    rows = {v.id: v for v in db.scalars(select(RuleVersion).where(RuleVersion.id.in_(ids)))}
    return [(version_id, rule_from_body(rows[version_id].body)) for version_id in ids]


def export_ruleset(db: Session, code: str) -> RulesetFile | None:
    latest = latest_ruleset_version(db, code)
    if latest is None:
        return None
    ruleset = latest.ruleset
    meta = RulesetMeta(
        code=ruleset.code, title=ruleset.title, law=ruleset.law, version=latest.version
    )
    return RulesetFile(ruleset=meta, rules=[rule for _, rule in ruleset_rules(db, latest)])
