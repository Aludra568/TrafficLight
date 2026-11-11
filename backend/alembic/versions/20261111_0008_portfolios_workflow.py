"""Портфели закупок и независимые статусы обработки замечаний."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("handling_status", sa.String(32), server_default="new", nullable=False),
    )
    op.create_check_constraint(
        "handlingstatus",
        "findings",
        "handling_status IN ('new', 'in_progress', 'resolved', 'not_applicable')",
    )
    op.add_column(
        "findings", sa.Column("handling_version", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("findings", sa.Column("handling_updated_at", sa.DateTime(timezone=True)))
    op.add_column("findings", sa.Column("handling_updated_by", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_findings_handling_updated_by_users",
        "findings",
        "users",
        ["handling_updated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_findings_handling_updated_by", "findings", ["handling_updated_by"])
    op.create_table(
        "portfolios",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "ruleset_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ruleset_versions.id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_portfolio_request"),
    )
    op.create_index("ix_portfolios_owner_id", "portfolios", ["owner_id"])
    op.create_index("ix_portfolios_ruleset_version_id", "portfolios", ["ruleset_version_id"])
    op.create_table(
        "portfolio_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "portfolio_id",
            UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_name", sa.String(500), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("source_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("check_id", UUID(as_uuid=True), sa.ForeignKey("checks.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), server_default="waiting", nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("portfolio_id", "position", name="uq_portfolio_position"),
        sa.CheckConstraint("source_type IN ('file', 'eis')", name="source_type"),
        sa.CheckConstraint(
            "status IN ('waiting', 'starting', 'checking', 'failed')", name="status"
        ),
        sa.CheckConstraint(
            "position >= 0 AND attempts >= 0 AND source_bytes >= 0", name="nonnegative"
        ),
    )
    op.create_index("ix_portfolio_items_portfolio_id", "portfolio_items", ["portfolio_id"])
    op.create_index("ix_portfolio_items_check_id", "portfolio_items", ["check_id"])


def downgrade() -> None:
    op.drop_table("portfolio_items")
    op.drop_table("portfolios")
    op.drop_index("ix_findings_handling_updated_by", table_name="findings")
    op.drop_constraint("fk_findings_handling_updated_by_users", "findings", type_="foreignkey")
    op.drop_constraint(op.f("ck_findings_handlingstatus"), "findings", type_="check")
    for name in (
        "handling_updated_by",
        "handling_updated_at",
        "handling_version",
        "handling_status",
    ):
        op.drop_column("findings", name)
