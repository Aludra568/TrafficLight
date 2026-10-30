import uuid
from pathlib import Path

import jwt
import pytest

from svetofor.api.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from svetofor.db.enums import Role


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("секрет-123")
    assert hashed != "секрет-123"
    assert verify_password("секрет-123", hashed)
    assert not verify_password("не тот", hashed)


def test_verify_tolerates_garbage_hash() -> None:
    assert not verify_password("x", "не-хэш")


def test_token_roundtrip() -> None:
    uid = uuid.uuid4()
    claims = decode_access_token(create_access_token(uid, Role.METHODOLOGIST))
    assert claims.user_id == uid
    assert claims.role is Role.METHODOLOGIST


def test_expired_token_rejected() -> None:
    token = create_access_token(uuid.uuid4(), Role.SPECIALIST, ttl_minutes=-1)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_token_signed_with_other_secret_rejected() -> None:
    other_secret = "чужой-секрет-достаточной-длины-для-hs256"
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": "admin"}, other_secret, algorithm="HS256"
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_default_jwt_secret_replaced_by_generated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Заглушку из репозитория знает каждый — вместо неё случайный секрет в томе данных."""
    from svetofor.api.security import jwt_secret
    from svetofor.core.config import DEFAULT_JWT_SECRET, get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", DEFAULT_JWT_SECRET)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    jwt_secret.cache_clear()
    try:
        first = jwt_secret()
        assert first != DEFAULT_JWT_SECRET and len(first) == 64
        assert (tmp_path / "jwt_secret").read_text(encoding="utf-8") == first
        jwt_secret.cache_clear()
        assert jwt_secret() == first  # переживает перезапуск: тот же файл
        monkeypatch.setattr(settings, "jwt_secret", "")  # пустой JWT_SECRET= из .env
        jwt_secret.cache_clear()
        assert jwt_secret() == first
    finally:
        jwt_secret.cache_clear()


def test_unwritable_data_dir_never_falls_back_to_public_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Том не открыт на запись: подпись не заглушкой из репозитория, а случайным секретом."""
    from svetofor.api import security
    from svetofor.core.config import DEFAULT_JWT_SECRET, get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", DEFAULT_JWT_SECRET)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    def refuse(_: Path) -> str:
        raise PermissionError("read-only volume")

    monkeypatch.setattr(security, "_stored_secret", refuse)
    security.jwt_secret.cache_clear()
    try:
        secret = security.jwt_secret()
        assert secret != DEFAULT_JWT_SECRET and len(secret) == 64
        forged = jwt.encode(
            {"sub": str(uuid.uuid4()), "role": "admin", "exp": 4102444800},
            DEFAULT_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_access_token(forged)
    finally:
        security.jwt_secret.cache_clear()


def test_secret_file_created_once_and_damaged_file_replaced(tmp_path: Path) -> None:
    from svetofor.api.security import _stored_secret

    path = tmp_path / "jwt_secret"
    first = _stored_secret(path)
    assert _stored_secret(path) == first  # второй процесс читает тот же секрет
    path.write_text("short", encoding="utf-8")
    replaced = _stored_secret(path)
    assert replaced != first and len(replaced) == 64


def test_secret_created_concurrently_is_the_same_everywhere(tmp_path: Path) -> None:
    """Код-ревью 2026-10-08: читатель удалял файл, который другой процесс ещё дописывал, —
    процессы API получали разные секреты и не принимали токены друг друга."""
    from concurrent.futures import ThreadPoolExecutor

    from svetofor.api.security import _stored_secret

    path = tmp_path / "data" / "jwt_secret"
    for _ in range(20):
        path.unlink(missing_ok=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            secrets_seen = set(pool.map(lambda _: _stored_secret(path), range(8)))
        assert len(secrets_seen) == 1
        assert path.read_text(encoding="utf-8") in secrets_seen
    assert not list(path.parent.glob(".jwt_secret-*"))  # временные файлы убраны
