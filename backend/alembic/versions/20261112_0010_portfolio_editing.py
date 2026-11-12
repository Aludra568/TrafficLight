"""Версии редактирования и идемпотентные операции портфеля."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "portfolios", sa.Column("title_version", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "portfolio_items", sa.Column("version", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("portfolio_items", sa.Column("edit_token", sa.String(36)))
    op.add_column("portfolio_items", sa.Column("edit_error", sa.Text()))
    op.add_column(
        "portfolio_items",
        sa.Column("replace_source", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_table(
        "portfolio_operations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "portfolio_id",
            sa.UUID(),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("portfolio_id", "request_key", name="uq_portfolio_operation"),
    )
    op.create_index(
        "ix_portfolio_operations_portfolio_id", "portfolio_operations", ["portfolio_id"]
    )


def downgrade():
    op.drop_table("portfolio_operations")
    for name in ("replace_source", "edit_error", "edit_token", "version"):
        op.drop_column("portfolio_items", name)
    op.drop_column("portfolios", "title_version")
