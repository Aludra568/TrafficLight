"""facts: канал извлечения (модель / грамматика / оба) и расхождение каналов

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "facts",
        sa.Column("source", sa.String(length=16), server_default="llm", nullable=False),
    )
    op.add_column(
        "facts", sa.Column("conflict", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("facts", "conflict")
    op.drop_column("facts", "source")
