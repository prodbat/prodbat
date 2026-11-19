"""signed webhooks and GitHub issues

Revision ID: c8f3a6d2e915
Revises: b5e1c9d3f702
Create Date: 2026-11-19 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c8f3a6d2e915'
down_revision: Union[str, Sequence[str], None] = 'b5e1c9d3f702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROJECT_COLUMNS = [
    sa.Column('webhook_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('webhook_url', sa.Text(), nullable=True),
    sa.Column('webhook_secret', sa.Text(), nullable=True),
    sa.Column('webhook_error', sa.Text(), nullable=True),
    sa.Column('webhook_failing_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('webhook_gone', sa.Integer(), server_default='0', nullable=False),
    sa.Column('github_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('github_installation_id', sa.BigInteger(), nullable=True),
    sa.Column('github_account', sa.Text(), nullable=True),
    sa.Column('github_repos', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('github_repo', sa.Text(), nullable=True),
    sa.Column('github_state', sa.Text(), nullable=True),
    sa.Column('github_state_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('github_look', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('github_mention', sa.Text(), nullable=True),
    sa.Column('github_assignees', sa.Text(), nullable=True),
    sa.Column('github_error', sa.Text(), nullable=True),
    sa.Column('github_note', sa.Text(), nullable=True),
]


def upgrade() -> None:
    for column in PROJECT_COLUMNS:
        op.add_column('projects', column)
    op.add_column('incidents', sa.Column('github_issue', sa.BigInteger(), nullable=True))
    op.add_column('incidents', sa.Column('github_said', sa.Text(), nullable=True))
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('incident_id', sa.BigInteger(), nullable=True),
        sa.Column('event', sa.Text(), nullable=False),
        sa.Column('delivery_id', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), server_default='{}', nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('gave_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_status', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('delivery_id'),
    )
    op.create_index('ix_webhook_deliveries_due', 'webhook_deliveries', ['next_attempt_at'], unique=False,
                    postgresql_where=sa.text('sent_at is null and gave_up_at is null'))
    op.create_index('ix_webhook_deliveries_project', 'webhook_deliveries', ['project_id', 'created_at'], unique=False)
    op.create_table(
        'github_posts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('incident_id', sa.BigInteger(), nullable=True),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_github_posts_unsent', 'github_posts', ['created_at'], unique=False,
                    postgresql_where=sa.text('sent_at is null'))


def downgrade() -> None:
    op.drop_index('ix_github_posts_unsent', table_name='github_posts', postgresql_where=sa.text('sent_at is null'))
    op.drop_table('github_posts')
    op.drop_index('ix_webhook_deliveries_project', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_due', table_name='webhook_deliveries',
                  postgresql_where=sa.text('sent_at is null and gave_up_at is null'))
    op.drop_table('webhook_deliveries')
    op.drop_column('incidents', 'github_said')
    op.drop_column('incidents', 'github_issue')
    for column in reversed(PROJECT_COLUMNS):
        op.drop_column('projects', column.name)
