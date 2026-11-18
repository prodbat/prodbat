"""text messages: a confirmed number per project, and an outbox

Revision ID: a41d9c7e2f10
Revises: 7c2e1f0a9b31
Create Date: 2026-11-18 19:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a41d9c7e2f10'
down_revision: Union[str, Sequence[str], None] = '7c2e1f0a9b31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('sms_to', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('sms_pending', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('sms_code_hash', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('sms_code_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('projects', sa.Column('sms_enabled', sa.Boolean(), server_default='true', nullable=False))
    op.create_table(
        'texts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=True),
        sa.Column('to', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_texts_unsent', 'texts', ['created_at'], postgresql_where=sa.text('sent_at is null'))


def downgrade() -> None:
    op.drop_index('ix_texts_unsent', table_name='texts', postgresql_where=sa.text('sent_at is null'))
    op.drop_table('texts')
    for name in ('sms_enabled', 'sms_code_at', 'sms_code_hash', 'sms_pending', 'sms_to'):
        op.drop_column('projects', name)
