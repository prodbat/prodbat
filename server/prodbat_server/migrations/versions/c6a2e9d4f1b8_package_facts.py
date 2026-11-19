"""package_facts and eol_facts: what the registries say about a package version or release line, shared by every site

Revision ID: c6a2e9d4f1b8
Revises: b3d8f1a6c720
Create Date: 2026-11-19 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c6a2e9d4f1b8'
down_revision: Union[str, Sequence[str], None] = 'b3d8f1a6c720'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'package_facts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('ecosystem', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('version', sa.Text(), nullable=False),
        sa.Column('serious', sa.JSON(), server_default='[]', nullable=False),
        sa.Column('issues', sa.JSON(), server_default='[]', nullable=False),
        sa.Column('advisories_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('latest', sa.Text(), nullable=True),
        sa.Column('latest_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deprecated', sa.Text(), nullable=True),
        sa.Column('deprecated_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ecosystem', 'name', 'version'),
    )
    op.create_table(
        'eol_facts',
        sa.Column('product', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('product'),
    )


def downgrade() -> None:
    op.drop_table('eol_facts')
    op.drop_table('package_facts')
