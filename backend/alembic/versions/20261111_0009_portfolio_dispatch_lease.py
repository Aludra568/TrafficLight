"""Единственная возобновляемая цепочка диспетчеризации портфеля."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portfolios", sa.Column("dispatch_token", sa.String(36)))
    op.add_column("portfolios", sa.Column("dispatch_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("portfolios", "dispatch_until")
    op.drop_column("portfolios", "dispatch_token")
