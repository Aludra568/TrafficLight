"""Надёжность и трассировка: попытки обработки документа, «пульс» проверки, детали исхода правила

documents.attempts — сколько раз документ брался в обработку (падение воркера не зациклит очередь);
checks.heartbeat_at — последняя активность проверки (сторож помечает зависшие проверки);
rule_results.details — что видела модель и чем подтверждён исход (фрагменты, цитата «в порядке»);
documents.signals — сигналы оформления файла (скрытый текст, принятые исправления Word).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "documents",
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("checks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "rule_results",
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rule_results", "details")
    op.drop_column("checks", "heartbeat_at")
    op.drop_column("documents", "signals")
    op.drop_column("documents", "attempts")
