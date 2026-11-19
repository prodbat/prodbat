"""when the package list was last sent, for the dependencies table on the page

Revision ID: f9a2c6e1b8d4
Revises: e7c3b9a1d5f4
Create Date: 2026-11-19 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f9a2c6e1b8d4'
down_revision: Union[str, Sequence[str], None] = 'e7c3b9a1d5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('dependencies_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'dependencies_sent_at')
