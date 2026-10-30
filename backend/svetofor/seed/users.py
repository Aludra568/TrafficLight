"""Демо-пользователи. Пароль — DEMO_USERS_PASSWORD; состояние сверяется при каждом старте.

Раньше повторный запуск ничего не менял: выключенный DEMO_USERS или новый DEMO_USERS_PASSWORD
не действовали на уже созданные учётки — они оставались с паролем «demo», а предупреждение
при старте пропадало. Методолог меняет правила — то есть вердикты для всех.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from svetofor.api.security import hash_password, verify_password
from svetofor.db.enums import Role
from svetofor.db.models import User

DEMO_USERS: tuple[tuple[str, str, Role], ...] = (
    ("specialist@demo.ru", "Специалист по закупкам (демо)", Role.SPECIALIST),
    ("methodologist@demo.ru", "Методолог (демо)", Role.METHODOLOGIST),
    ("admin@demo.ru", "Администратор (демо)", Role.ADMIN),
)


def seed_users(db: Session, password: str) -> int:
    """Создаёт отсутствующих демо-пользователей. Возвращает число созданных."""
    existing = set(db.scalars(select(User.email)).all())
    created = 0
    for email, full_name, role in DEMO_USERS:
        if email in existing:
            continue
        db.add(
            User(email=email, full_name=full_name, role=role, password_hash=hash_password(password))
        )
        created += 1
    db.flush()
    return created


def sync_demo_users(db: Session, enabled: bool, password: str) -> dict[str, int]:
    """Привести демо-учётки к настройкам: включены — создать недостающие и сменить пароль на
    заданный; выключены — отключить (вход и выданные токены перестают действовать)."""
    users = {
        u.email: u
        for u in db.scalars(select(User).where(User.email.in_([e for e, _, _ in DEMO_USERS])))
    }
    result = {"created": 0, "password_updated": 0, "disabled": 0}
    if not enabled:
        for user in users.values():
            if user.is_active:
                user.is_active = False
                result["disabled"] += 1
        db.flush()
        return result
    result["created"] = seed_users(db, password)
    for user in users.values():
        if not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)
            result["password_updated"] += 1
    db.flush()
    return result
