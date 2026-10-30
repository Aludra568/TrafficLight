from __future__ import annotations

import math
import secrets

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from svetofor.api.deps import CurrentUser, DbSession
from svetofor.api.errors import AppError
from svetofor.api.security import create_access_token, hash_password, verify_password
from svetofor.api.throttle import LoginThrottle
from svetofor.core.config import get_settings
from svetofor.db.models import User
from svetofor.schemas.auth import DemoAccess, LoginRequest, TokenOut, UserOut

router = APIRouter(prefix="/api/auth", tags=["Вход"])
_DUMMY_HASH = hash_password(secrets.token_hex(16))  # с ним сверяется пароль несуществующей почты


def _throttle(request: Request) -> LoginThrottle:
    throttle = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = request.app.state.login_throttle = LoginThrottle()
    return throttle


@router.post("/login", response_model=TokenOut, summary="Вход по email и паролю")
def login(body: LoginRequest, request: Request, db: DbSession) -> TokenOut:
    email = body.email.strip().lower()
    client = request.client.host if request.client else "unknown"
    throttle = _throttle(request)
    wait = throttle.wait_seconds(email, client)
    if wait:
        minutes = max(1, math.ceil(wait / 60))
        raise AppError(
            "too_many_attempts",
            f"Слишком много неудачных попыток входа. Попробуйте через {minutes} мин.",
            429,
        )
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    # пароль сверяется и для несуществующей почты: иначе ответ без Argon2 (~0 мс) против
    # ответа с ним (~70 мс) выдаёт, какие адреса зарегистрированы
    password_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(body.password, password_hash)
    if user is None or not user.is_active or not password_ok:
        throttle.failed(email, client)
        # Одинаковый ответ для «нет пользователя» и «неверный пароль»:
        # не раскрываем, какие email зарегистрированы.
        raise AppError("invalid_credentials", "Неверный email или пароль", 401)
    throttle.succeeded(email)
    token = create_access_token(user.id, user.role)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/demo", response_model=DemoAccess, summary="Демо-учётки для страницы входа")
def demo_access() -> DemoAccess:
    """Выключенные демо-учётки страница входа не показывает, а свой пароль не подставляет:
    раньше она всегда предлагала «demo» — и на стенде без демо-доступа тоже."""
    settings = get_settings()
    return DemoAccess(
        enabled=settings.demo_users,
        default_password=settings.demo_users and settings.demo_users_password == "demo",  # noqa: S105
    )
