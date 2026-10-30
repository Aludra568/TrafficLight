"""Зависимости FastAPI: сессия БД, текущий пользователь, проверка роли."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from svetofor.api.errors import forbidden, unauthorized
from svetofor.api.security import InvalidTokenError, decode_access_token, decode_ticket
from svetofor.db.enums import Role
from svetofor.db.models import User
from svetofor.db.session import get_db, get_sessionmaker

DbSession = Annotated[Session, Depends(get_db)]


def get_session_factory() -> Callable[[], Session]:
    """Фабрика сессий для кода, которому нужны свои транзакции (SSE, постановка в очередь)."""
    return get_sessionmaker()


SessionFactoryDep = Annotated[Callable[[], Session], Depends(get_session_factory)]

_bearer = HTTPBearer(auto_error=False)

# Иерархия: каждая роль включает права предыдущих.
_ROLE_LEVEL = {Role.SPECIALIST: 1, Role.METHODOLOGIST: 2, Role.ADMIN: 3}


def _user_from_token(db: Session, token: str | None) -> User:
    if not token:
        raise unauthorized()
    try:
        claims = decode_access_token(token)
    except InvalidTokenError as exc:
        raise unauthorized("Сессия истекла или недействительна. Войдите снова") from exc
    user = db.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise unauthorized("Пользователь не найден или отключён")
    return user


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    return _user_from_token(db, credentials.credentials if credentials else None)


def user_for_events(
    check_id: uuid.UUID,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ticket: Annotated[
        str | None, Query(description="Билет событий: POST /api/checks/{id}/events/ticket")
    ] = None,
) -> User:
    """Для SSE: браузерный EventSource не умеет передавать заголовок авторизации.

    Вместо токена сессии в адресе — короткий билет только на события этой проверки.
    """
    if credentials:
        return _user_from_token(db, credentials.credentials)
    if not ticket:
        raise unauthorized()
    try:
        claims = decode_ticket(ticket, f"sse:{check_id}")
    except InvalidTokenError as exc:
        raise unauthorized("Билет событий истёк или выдан для другой проверки") from exc
    user = db.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise unauthorized("Пользователь не найден или отключён")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def has_role(user: User, minimum: Role) -> bool:
    return _ROLE_LEVEL[user.role] >= _ROLE_LEVEL[minimum]


def require_role(minimum: Role) -> Callable[[User], User]:
    def _check(user: CurrentUser) -> User:
        if not has_role(user, minimum):
            raise forbidden()
        return user

    return _check


Methodologist = Annotated[User, Depends(require_role(Role.METHODOLOGIST))]
Admin = Annotated[User, Depends(require_role(Role.ADMIN))]
