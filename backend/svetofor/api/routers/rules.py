"""Правила и наборы.

Новое правило — без изменения кода: описание проверяется, сохраняется новой версией и сразу
входит в новую версию набора. Старые проверки остаются на своих версиях.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from svetofor.api.access import sign_task_id, task_id_for
from svetofor.api.deps import CurrentUser, DbSession, Methodologist
from svetofor.api.errors import AppError, not_found
from svetofor.api.routers.checks import send_task
from svetofor.core.config import get_settings
from svetofor.db.enums import FeedbackVerdict, FindingStatus, RuleStatus, Severity
from svetofor.db.models import (
    AuditLog,
    Feedback,
    Finding,
    Ruleset,
    RulesetVersion,
    RuleVersion,
    User,
)
from svetofor.db.models import Rule as RuleRow
from svetofor.rules.loader import RulesetError, dump_ruleset, load_ruleset_text, parse_rule
from svetofor.rules.model import CATEGORY_TITLES, Rule, ThresholdRule
from svetofor.rules.store import (
    export_ruleset,
    import_ruleset,
    latest_ruleset_version,
    publish_current,
    rule_from_body,
    save_rule,
)
from svetofor.schemas.rules import (
    RuleDetail,
    RuleIn,
    RuleListItem,
    RuleSaved,
    RulesetOut,
    RuleVersionOut,
    SandboxIn,
    SandboxResult,
    SandboxTask,
    VerifyIn,
)

router = APIRouter(prefix="/api", tags=["Правила"])


def _invalid(exc: RulesetError) -> AppError:
    return AppError(
        "invalid_rule", "Правило заполнено с ошибками", 422, details={"problems": exc.problems}
    )


def _worst_severity(rule: Rule) -> str | None:
    if isinstance(rule, ThresholdRule):
        severities = {c.severity for c in rule.conditions}
        if rule.on_missing:
            severities.add(rule.on_missing.severity)
        return Severity.RED.value if Severity.RED in severities else Severity.YELLOW.value
    severity = getattr(rule, "severity", None)
    return severity.value if severity else None


def _oldest_edition(rule: Rule) -> date | None:
    dates = [ref.edition_checked for ref in rule.legal_basis if ref.edition_checked]
    return min(dates) if dates else None


def _current(db: DbSession, code: str) -> tuple[RuleRow, RuleVersion]:
    row = db.scalar(select(RuleRow).where(RuleRow.code == code, RuleRow.is_deleted.is_(False)))
    version = (
        db.get(RuleVersion, row.current_version_id) if row and row.current_version_id else None
    )
    if row is None or version is None:
        raise not_found("Правило")
    return row, version


@router.get("/rules", response_model=list[RuleListItem], summary="Список правил")
def list_rules(
    _: CurrentUser,
    db: DbSession,
    category: str | None = None,
    law: str | None = None,
    status: RuleStatus | None = None,
    type: str | None = None,
    q: Annotated[str | None, Query(description="Поиск по коду и названию")] = None,
) -> list[RuleListItem]:
    since = datetime.now(UTC) - timedelta(days=30)
    triggers = dict(
        db.execute(
            select(Finding.rule_code, func.count())
            .where(Finding.created_at >= since, Finding.status == FindingStatus.SHOWN)
            .group_by(Finding.rule_code)
        ).all()
    )
    marks: Counter[tuple[str | None, FeedbackVerdict]] = Counter(
        (f.rule_code, f.verdict) for f in db.scalars(select(Feedback))
    )
    stale_after = get_settings().legal_stale_days
    items = []
    for row in db.scalars(
        select(RuleRow).where(RuleRow.is_deleted.is_(False)).order_by(RuleRow.code)
    ):
        version = db.get(RuleVersion, row.current_version_id) if row.current_version_id else None
        if version is None:
            continue
        rule = rule_from_body(version.body)
        if (category and rule.category.value != category) or (law and law not in rule.law):
            continue
        if (status and version.status is not status) or (type and rule.type != type):
            continue
        if q and q.lower() not in f"{rule.id} {rule.title}".lower():
            continue
        correct = marks[(rule.id, FeedbackVerdict.CORRECT)]
        wrong = marks[(rule.id, FeedbackVerdict.FALSE_POSITIVE)]
        edition = _oldest_edition(rule)
        items.append(
            RuleListItem(
                code=rule.id,
                title=rule.title,
                category=rule.category.value,
                category_title=CATEGORY_TITLES[rule.category],
                type=rule.type,
                severity=_worst_severity(rule),
                status=version.status,
                law=rule.law,
                applies_to=rule.applies_to,
                enabled=rule.enabled,
                version=version.version,
                updated_at=version.created_at,
                verified_by=version.verified_by,
                edition_checked=edition,
                stale=bool(edition and (date.today() - edition).days > stale_after),
                triggers_30d=triggers.get(rule.id, 0),
                precision=round(correct / (correct + wrong), 2) if correct + wrong else None,
                feedback_count=correct + wrong,
            )
        )
    return items


@router.get("/rules/{code}", response_model=RuleDetail, summary="Правило и история версий")
def get_rule(code: str, _: CurrentUser, db: DbSession) -> RuleDetail:
    row, current = _current(db, code)
    versions = list(
        db.scalars(
            select(RuleVersion)
            .where(RuleVersion.rule_id == row.id)
            .order_by(RuleVersion.version.desc())
        )
    )
    authors = {
        u.id: u.email
        for u in db.scalars(
            select(User).where(User.id.in_([v.author_id for v in versions if v.author_id]))
        )
    }
    return RuleDetail(
        code=code,
        body=current.body,
        current_version=current.version,
        versions=[
            RuleVersionOut(
                version=v.version,
                created_at=v.created_at,
                status=v.status,
                change_note=v.change_note,
                author=authors.get(v.author_id) if v.author_id else None,
            )
            for v in versions
        ],
    )


def _save_and_publish(
    db: DbSession, user: User, rule: Rule, note: str | None, publish: bool, action: str
) -> RuleSaved:
    version = save_rule(db, rule, author_id=user.id, note=note)
    published = (
        publish_current(db, get_settings().default_ruleset, author_id=user.id) if publish else None
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            entity="rule",
            entity_id=rule.id,
            payload={"version": version.version, "note": note},
        )
    )
    db.commit()
    return RuleSaved(
        code=rule.id,
        version=version.version,
        ruleset_version=published.version if published else None,
    )


@router.post("/rules", response_model=RuleSaved, status_code=201, summary="Создать правило")
def create_rule(
    body: RuleIn, user: Methodologist, db: DbSession, publish: bool = True
) -> RuleSaved:
    try:
        rule = parse_rule(body.rule)
    except RulesetError as exc:
        raise _invalid(exc) from exc
    if db.scalar(select(RuleRow.id).where(RuleRow.code == rule.id, RuleRow.is_deleted.is_(False))):
        raise AppError("rule_exists", f"Правило {rule.id} уже есть — измените его", 409)
    return _save_and_publish(db, user, rule, body.change_note or "новое правило", publish, "create")


@router.put("/rules/{code}", response_model=RuleSaved, summary="Новая версия правила")
def update_rule(
    code: str, body: RuleIn, user: Methodologist, db: DbSession, publish: bool = True
) -> RuleSaved:
    _current(db, code)
    try:
        rule = parse_rule(body.rule)
    except RulesetError as exc:
        raise _invalid(exc) from exc
    if rule.id != code:
        raise AppError("code_mismatch", "Код правила менять нельзя — создайте новое правило", 400)
    return _save_and_publish(db, user, rule, body.change_note, publish, "update")


@router.post("/rules/{code}/verify", response_model=RuleSaved, summary="Проверено юристом")
def verify_rule(code: str, body: VerifyIn, user: Methodologist, db: DbSession) -> RuleSaved:
    _, current = _current(db, code)
    rule = rule_from_body(current.body).model_copy(
        update={
            "status": RuleStatus.VERIFIED,
            "verified_by": body.verified_by,
            "verified_at": body.verified_at or date.today(),
        }
    )
    return _save_and_publish(db, user, rule, f"проверено: {body.verified_by}", True, "verify")


@router.post("/rules/test", response_model=SandboxTask, status_code=202, summary="Песочница")
def sandbox(body: SandboxIn, user: Methodologist, db: DbSession) -> SandboxTask:
    """Только методолог: песочница вызывает модель в обход лимита одновременных проверок."""
    from svetofor.tasks.rules import sandbox_task

    if body.rule is not None:
        raw = body.rule
    elif body.rule_code:
        raw = _current(db, body.rule_code)[1].body
    else:
        raise AppError("rule_required", "Укажите правило: код или описание из редактора", 400)
    try:
        rule = parse_rule(raw)
    except RulesetError as exc:
        raise _invalid(exc) from exc
    doc_type = body.doc_type.value if body.doc_type else None
    task = send_task(sandbox_task, rule.model_dump(mode="json"), body.text, doc_type)
    # номер подписан вместе с id пользователя: результат получит только тот, кто запустил
    return SandboxTask(task_id=sign_task_id(task.id, user.id))


@router.get("/rules/test/{task_id}", response_model=SandboxResult, summary="Результат песочницы")
def sandbox_result(task_id: str, user: Methodologist) -> SandboxResult:
    from celery.result import AsyncResult

    from svetofor.tasks.celery_app import celery_app

    result = AsyncResult(task_id_for(task_id, user), app=celery_app)
    if result.successful():
        return SandboxResult(status="done", result=result.result)
    if result.failed():
        return SandboxResult(status="failed", error="Не удалось проверить правило на тексте")
    return SandboxResult(status="pending")


# ─────────────────────────── Наборы ───────────────────────────


def _ruleset_out(version: RulesetVersion) -> RulesetOut:
    ruleset = version.ruleset
    return RulesetOut(
        code=ruleset.code,
        title=ruleset.title,
        law=ruleset.law,
        version=version.version,
        rules=len(version.rule_version_ids),
        published_at=version.published_at,
    )


@router.get("/rulesets", response_model=list[RulesetOut], summary="Наборы правил")
def list_rulesets(_: CurrentUser, db: DbSession) -> list[RulesetOut]:
    result = []
    for ruleset in db.scalars(select(Ruleset).order_by(Ruleset.code)):
        latest = latest_ruleset_version(db, ruleset.code)
        if latest is not None:
            result.append(_ruleset_out(latest))
    return result


@router.post("/rulesets/{code}/publish", response_model=RulesetOut, summary="Опубликовать набор")
def publish(code: str, user: Methodologist, db: DbSession) -> RulesetOut:
    version = publish_current(db, code, author_id=user.id)
    if version is None:
        raise not_found("Набор правил")
    db.commit()
    return _ruleset_out(version)


@router.get(
    "/rulesets/{code}/export", summary="Выгрузить набор в YAML", response_class=PlainTextResponse
)
def export(code: str, _: CurrentUser, db: DbSession) -> PlainTextResponse:
    ruleset = export_ruleset(db, code)
    if ruleset is None:
        raise not_found("Набор правил")
    headers = {"Content-Disposition": f'attachment; filename="{code}.yaml"'}
    return PlainTextResponse(
        dump_ruleset(ruleset), media_type="application/x-yaml", headers=headers
    )


@router.post("/rulesets/import", response_model=RulesetOut, summary="Загрузить набор из YAML")
def import_yaml(
    user: Methodologist, db: DbSession, file: Annotated[UploadFile, File()]
) -> RulesetOut:
    raw = file.file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise AppError("too_large", "Файл набора правил больше 5 МБ", 413)
    try:
        ruleset = load_ruleset_text(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise AppError("bad_encoding", "Файл должен быть в кодировке UTF-8", 400) from exc
    except RulesetError as exc:
        raise _invalid(exc) from exc
    version = import_ruleset(db, ruleset, author_id=user.id, note=f"импорт {file.filename}")
    db.add(
        AuditLog(user_id=user.id, action="import", entity="ruleset", entity_id=ruleset.ruleset.code)
    )
    db.commit()
    return _ruleset_out(version)
