"""Серый цвет пакета: замечаний нет, но проверена меньшая часть правил («риски не оценены»)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_checks_light"), "checks", type_="check")
    op.create_check_constraint(
        op.f("ck_checks_light"), "checks", "light IN ('red', 'yellow', 'green', 'gray')"
    )


def downgrade() -> None:
    op.execute("UPDATE checks SET light = NULL WHERE light = 'gray'")
    op.drop_constraint(op.f("ck_checks_light"), "checks", type_="check")
    op.create_check_constraint(
        op.f("ck_checks_light"), "checks", "light IN ('red', 'yellow', 'green')"
    )
