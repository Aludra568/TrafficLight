from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from svetofor.db.enums import Role


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — тип токена по OAuth2, не секрет
    user: UserOut


class DemoAccess(BaseModel):
    """Что показать на странице входа: демо-учётки включены и пароль ли по умолчанию («demo»)."""

    enabled: bool
    default_password: bool
