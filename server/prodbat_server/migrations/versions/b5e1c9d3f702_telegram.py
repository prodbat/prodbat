"""alerts in Telegram

Revision ID: b5e1c9d3f702
Revises: a9d3e7b1c460
Create Date: 2026-11-19 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b5e1c9d3f702'
down_revision: Union[str, Sequence[str], None] = 'a9d3e7b1c460'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('telegram_enabled', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('projects', sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True))
    op.add_column('projects', sa.Column('telegram_chat', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('telegram_code', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('telegram_code_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('projects', sa.Column('telegram_error', sa.Text(), nullable=True))
    op.create_unique_constraint('projects_telegram_code_key', 'projects', ['telegram_code'])
    op.add_column('incidents', sa.Column('telegram_message_id', sa.BigInteger(), nullable=True))
    op.create_table(
        'telegram_messages',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('incident_id', sa.BigInteger(), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_telegram_messages_unsent', 'telegram_messages', ['created_at'], unique=False,
                    postgresql_where=sa.text('sent_at is null'))


def downgrade() -> None:
    op.drop_index('ix_telegram_messages_unsent', table_name='telegram_messages',
                  postgresql_where=sa.text('sent_at is null'))
    op.drop_table('telegram_messages')
    op.drop_column('incidents', 'telegram_message_id')
    op.drop_constraint('projects_telegram_code_key', 'projects', type_='unique')
    for name in ['telegram_error', 'telegram_code_at', 'telegram_code', 'telegram_chat', 'telegram_chat_id',
                 'telegram_enabled']:
        op.drop_column('projects', name)
