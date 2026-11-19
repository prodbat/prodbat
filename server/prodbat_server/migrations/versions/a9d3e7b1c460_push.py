"""notifications on the owner's devices (web push)

Revision ID: a9d3e7b1c460
Revises: f4c8a2e6b377
Create Date: 2026-11-19 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9d3e7b1c460'
down_revision: Union[str, Sequence[str], None] = 'f4c8a2e6b377'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('push_enabled', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('projects', sa.Column('push_error', sa.Text(), nullable=True))
    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.Text(), nullable=False),
        sa.Column('auth', sa.Text(), nullable=False),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )
    op.create_table(
        'push_messages',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('incident_id', sa.BigInteger(), nullable=True),
        sa.Column('subscription_id', sa.BigInteger(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('urgent', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subscription_id'], ['push_subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_push_messages_unsent', 'push_messages', ['created_at'], unique=False,
                    postgresql_where=sa.text('sent_at is null'))


def downgrade() -> None:
    op.drop_index('ix_push_messages_unsent', table_name='push_messages', postgresql_where=sa.text('sent_at is null'))
    op.drop_table('push_messages')
    op.drop_table('push_subscriptions')
    op.drop_column('projects', 'push_error')
    op.drop_column('projects', 'push_enabled')
