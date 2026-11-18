"""the owner's time zone

Revision ID: e2b6d4a8f155
Revises: d7a3c1f5e844
Create Date: 2026-11-18 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2b6d4a8f155'
down_revision: Union[str, Sequence[str], None] = 'd7a3c1f5e844'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('timezone', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'timezone')
