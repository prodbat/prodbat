"""alerts in Slack

Revision ID: f4c8a2e6b377
Revises: e2b6d4a8f155
Create Date: 2026-11-18 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f4c8a2e6b377'
down_revision: Union[str, Sequence[str], None] = 'e2b6d4a8f155'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ['slack_token', 'slack_webhook_url', 'slack_channel_id', 'slack_channel', 'slack_team', 'slack_state',
           'slack_error']


def upgrade() -> None:
    op.add_column('projects', sa.Column('slack_enabled', sa.Boolean(), server_default='true', nullable=False))
    for name in COLUMNS:
        op.add_column('projects', sa.Column(name, sa.Text(), nullable=True))
    op.add_column('incidents', sa.Column('slack_ts', sa.Text(), nullable=True))
    op.create_table(
        'slack_posts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('incident_id', sa.BigInteger(), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('blocks', sa.JSON(), server_default='[]', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_slack_posts_unsent', 'slack_posts', ['created_at'], unique=False,
                    postgresql_where=sa.text('sent_at is null'))


def downgrade() -> None:
    op.drop_index('ix_slack_posts_unsent', table_name='slack_posts', postgresql_where=sa.text('sent_at is null'))
    op.drop_table('slack_posts')
    op.drop_column('incidents', 'slack_ts')
    for name in COLUMNS + ['slack_enabled']:
        op.drop_column('projects', name)
