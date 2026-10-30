"""Кто какие пакеты видит.

Специалист — только свои пакеты (`Package.owner_id`), методолог и администратор — все:
методологу нужны результаты, чтобы править правила, администратор управляет стендом.
Чужой ресурс для специалиста — 404, как несуществующий: ответ не подтверждает, что он есть.
Пакет без владельца (загружен скриптом, владелец удалён) — только методологу и администратору.

Задача песочницы привязана к тому, кто её запустил: номер задачи, который получает клиент,
подписан ключом сервиса вместе с id пользователя. Чужой или подделанный номер — 404.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from sqlalchemy import Select
from sqlalchemy.orm import Session

from svetofor.api.deps import has_role
from svetofor.api.errors import AppError, not_found
from svetofor.api.security import jwt_secret
from svetofor.db.enums import Role
from svetofor.db.models import Check, Document, Finding, Package, User


def sees_all_packages(user: User) -> bool:
    """Методолог и выше (как `Methodologist` в `api/deps.py`) видят все пакеты."""
    return has_role(user, Role.METHODOLOGIST)


def visible_owner(user: User) -> uuid.UUID | None:
    """Чьи пакеты показывать: id специалиста или None — без ограничения (методолог, админ)."""
    return None if sees_all_packages(user) else user.id


def require_package_access(user: User, package: Package, what: str) -> None:
    """Единственная проверка доступа к пакету и всему, что в нём: проверкам, документам,
    замечаниям. Чужой пакет — «не найден», как несуществующий."""
    if sees_all_packages(user):
        return
    if package.owner_id is None or package.owner_id != user.id:
        raise not_found(what)


def only_visible[*Ts](statement: Select[*Ts], user: User) -> Select[*Ts]:
    """Запрос с `Package` в FROM — только пакеты, которые пользователь вправе видеть."""
    owner = visible_owner(user)
    return statement if owner is None else statement.where(Package.owner_id == owner)


def check_for(db: Session, check_id: uuid.UUID, user: User) -> Check:
    check = db.get(Check, check_id)
    if check is None:
        raise not_found("Проверка")
    require_package_access(user, check.package, "Проверка")
    return check


def document_for(db: Session, document_id: uuid.UUID, user: User) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise not_found("Документ")
    require_package_access(user, document.package, "Документ")
    return document


def finding_for(db: Session, finding_id: uuid.UUID, user: User) -> Finding:
    finding = db.get(Finding, finding_id)
    check = db.get(Check, finding.check_id) if finding else None
    if finding is None or check is None:
        raise not_found("Замечание")
    require_package_access(user, check.package, "Замечание")
    return finding


# ─────────────────────────── Задачи пользователя ───────────────────────────
# Песочница правил и загрузка из ЕИС: номер задачи подписан вместе с id пользователя и
# назначением — номер одной задачи не подходит к другой и не открывается другим пользователем.

_TASK_SCOPE = "sandbox"
EIS_SCOPE = "eis"


def _task_signature(task_id: str, user_id: uuid.UUID, scope: str) -> str:
    message = f"{scope}:{user_id}:{task_id}".encode()
    return hmac.new(jwt_secret().encode(), message, hashlib.sha256).hexdigest()[:32]


def sign_task_id(task_id: str, user_id: uuid.UUID, scope: str = _TASK_SCOPE) -> str:
    """Номер задачи для клиента: «<id задачи Celery>.<подпись>»."""
    return f"{task_id}.{_task_signature(task_id, user_id, scope)}"


def task_id_for(public_id: str, user: User, scope: str = _TASK_SCOPE) -> str:
    """Id задачи Celery из номера, выданного этому пользователю; иначе 404."""
    task_id, _, signature = public_id.rpartition(".")
    if not task_id or not hmac.compare_digest(signature, _task_signature(task_id, user.id, scope)):
        what = "Загрузка из ЕИС" if scope == EIS_SCOPE else "Задача проверки правила"
        raise AppError("not_found", f"{what} не найдена", 404)
    return task_id
