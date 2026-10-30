"""facts: смещения цитаты в тексте документа (для подсветки и доказательств в правилах)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("facts", sa.Column("char_start", sa.Integer(), nullable=True))
    op.add_column("facts", sa.Column("char_end", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("facts", "char_end")
    op.drop_column("facts", "char_start")
