"""when the coding agent said it fixed a problem, so a "needs a look" problem
clears 30 minutes after the fix instead of 24 hours after the last failure

Revision ID: b4f9d2a6c831
Revises: a3e8c1f7b264
Create Date: 2026-11-22 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b4f9d2a6c831'
down_revision: Union[str, Sequence[str], None] = 'a3e8c1f7b264'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('incidents', sa.Column('fix_claimed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('incidents', 'fix_claimed_at')
