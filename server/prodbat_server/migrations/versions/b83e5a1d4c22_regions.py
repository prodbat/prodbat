"""checks from several regions

Revision ID: b83e5a1d4c22
Revises: a41d9c7e2f10
Create Date: 2026-11-18 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b83e5a1d4c22'
down_revision: Union[str, Sequence[str], None] = 'a41d9c7e2f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('check_regions', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('site_checks', sa.Column('region', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('site_checks', 'region')
    op.drop_column('projects', 'check_regions')
