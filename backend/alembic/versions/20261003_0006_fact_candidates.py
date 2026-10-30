"""Кандидаты факта и снимок конвейера проверки

facts.candidates — другие значения поля в документе (этапы оплаты, цифры против прописи);
checks.pipeline — версии OCR/LibreOffice/разборщика, модель эмбеддингов, бюджеты и пороги.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03 23:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "facts", sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "checks", sa.Column("pipeline", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("checks", "pipeline")
    op.drop_column("facts", "candidates")
