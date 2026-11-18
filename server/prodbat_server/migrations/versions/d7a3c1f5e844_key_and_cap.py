"""a daily cap on events from the script

Revision ID: d7a3c1f5e844
Revises: c5f2b8e9a013
Create Date: 2026-11-18 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd7a3c1f5e844'
down_revision: Union[str, Sequence[str], None] = 'c5f2b8e9a013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('daily_event_cap', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'daily_event_cap')
