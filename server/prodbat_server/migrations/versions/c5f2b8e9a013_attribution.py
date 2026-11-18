"""payment-to-source attribution for subscriptions

Revision ID: c5f2b8e9a013
Revises: b83e5a1d4c22
Create Date: 2026-11-18 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5f2b8e9a013'
down_revision: Union[str, Sequence[str], None] = 'b83e5a1d4c22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('attribution_enabled', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('payments', sa.Column('subscription', sa.Text(), nullable=True))
    op.create_table(
        'attributions',
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('subscription', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'subscription'),
    )


def downgrade() -> None:
    op.drop_table('attributions')
    op.drop_column('payments', 'subscription')
    op.drop_column('projects', 'attribution_enabled')
