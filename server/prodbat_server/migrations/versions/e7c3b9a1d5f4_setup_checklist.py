"""The agent's setup checklist: skipped items and items waiting for data

Revision ID: e7c3b9a1d5f4
Revises: d1e4a7b2c9f3
Create Date: 2026-11-19 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7c3b9a1d5f4'
down_revision: Union[str, Sequence[str], None] = 'd1e4a7b2c9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('setup_skipped', sa.JSON(), server_default='[]', nullable=False))
    op.add_column('projects', sa.Column('setup_waiting', sa.JSON(), server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('projects', 'setup_waiting')
    op.drop_column('projects', 'setup_skipped')
