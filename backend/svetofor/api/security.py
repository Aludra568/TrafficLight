"""Пароли (Argon2) и токены доступа (JWT HS256).

Если JWT_SECRET не задан, при первом запуске создаётся случайный секрет и сохраняется в томе
данных: заглушку из репозитория знает каждый, и с ней можно подделать вход на любой установке.
Для событий проверки (SSE) выдаётся короткий «билет»: браузерный EventSource передаёт токен в
адресе, а адреса попадают в журналы прокси — полный токен сессии туда класть нельзя.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from svetofor.core.config import DEFAULT_JWT_SECRET, get_settings
from svetofor.core.logging import get_logger
from svetofor.db.enums import Role

_hasher = PasswordHasher()
_ALGORITHM = "HS256"
TICKET_TTL_S = 120
log = get_logger(__name__)


@lru_cache(maxsize=1)
def jwt_secret() -> str:
    """Секрет подписи: из настроек, а вместо заглушки — случайный, сохранённый в томе данных."""
    settings = get_settings()
    if settings.jwt_secret.strip() and settings.jwt_secret != DEFAULT_JWT_SECRET:
        return settings.jwt_secret  # пустая строка из .env — тоже «не задан», а не ключ
    path = settings.data_dir / "jwt_secret"
    try:
        return _stored_secret(path)
    except OSError:
        # Том недоступен на запись. Заглушку из репозитория знает каждый — с ней подделывается
        # вход администратора. Случайный секрет только в памяти: после перезапуска API всем
        # придётся войти заново, зато подделать вход нельзя
        log.warning("jwt_secret_ephemeral", hint="задайте JWT_SECRET в .env или откройте том")
        return secrets.token_hex(32)


def _stored_secret(path: Path) -> str:
    """Секрет из тома данных; нет — создать. Процессы API, стартующие одновременно, должны
    получить один и тот же секрет — иначе токены одного процесса не принимал бы другой.

    Файл появляется сразу целиком: секрет пишется во временный файл и ставится на место
    жёсткой ссылкой (`os.link` не перезаписывает существующий). Читатель никогда не видит
    недописанный файл, поэтому короткий файл — действительно испорченный, а не «ещё пишется».
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        if path.is_file():
            stored = path.read_text(encoding="utf-8").strip()
            if len(stored) >= 32:
                return stored
            path.unlink(missing_ok=True)  # испорченный файл (не недописанный) — создаём заново
        generated = secrets.token_hex(32)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".jwt_secret-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(generated)
            Path(temporary).chmod(0o600)
            os.link(temporary, path)
        except FileExistsError:
            continue  # другой процесс успел первым — прочитаем его секрет
        finally:
            Path(temporary).unlink(missing_ok=True)
        log.info("jwt_secret_generated", path=str(path))
        return generated
    raise OSError("секрет подписи не удалось ни прочитать, ни создать")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, UnicodeEncodeError):
        # Испорченный или не-ASCII хэш в БД — это «пароль не совпал», а не ошибка 500.
        return False


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    role: Role


class InvalidTokenError(Exception):
    pass


def create_access_token(user_id: uuid.UUID, role: Role, *, ttl_minutes: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iat": now,
        "exp": now + timedelta(minutes=ttl),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_ALGORITHM)


def decode_access_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])
        if "scope" in payload:  # билет событий — не токен сессии
            raise InvalidTokenError("билет не даёт доступа к API")
        return TokenClaims(user_id=uuid.UUID(payload["sub"]), role=Role(payload["role"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc


def create_ticket(user_id: uuid.UUID, role: Role, scope: str, ttl_s: int = TICKET_TTL_S) -> str:
    """Короткий билет на одно действие (например, «sse:<id проверки>»)."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_s),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_ALGORITHM)


def decode_ticket(token: str, scope: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])
        if payload.get("scope") != scope:
            raise InvalidTokenError("билет выдан для другого действия")
        return TokenClaims(user_id=uuid.UUID(payload["sub"]), role=Role(payload["role"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc
