"""GitHub connects through the App's user authorization

Revision ID: d1e4a7b2c9f3
Revises: c8f3a6d2e915
Create Date: 2026-11-19 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd1e4a7b2c9f3'
down_revision: Union[str, Sequence[str], None] = 'c8f3a6d2e915'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('github_login', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('github_pending', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'github_pending')
    op.drop_column('projects', 'github_login')
