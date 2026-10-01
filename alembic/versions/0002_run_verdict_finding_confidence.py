"""run verdict and warnings, finding confidence

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("verdict", sa.String(16), nullable=True))
    op.add_column("runs", sa.Column("warnings", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("confidence")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("warnings")
        batch.drop_column("verdict")
