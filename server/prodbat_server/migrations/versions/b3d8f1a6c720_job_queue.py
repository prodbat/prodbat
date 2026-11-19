"""job_queue: work the worker runs soon after it is enqueued, instead of finding it on a timer

Revision ID: b3d8f1a6c720
Revises: f9a2c6e1b8d4
Create Date: 2026-11-19 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3d8f1a6c720'
down_revision: Union[str, Sequence[str], None] = 'f9a2c6e1b8d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_queue',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('key', sa.Text(), server_default='', nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=True),
        sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed', sa.Boolean(), server_default='false', nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ux_job_queue_waiting', 'job_queue', ['kind', 'key'], unique=True,
                    postgresql_where=sa.text('finished_at is null and locked_at is null'))
    op.create_index('ix_job_queue_due', 'job_queue', ['run_after'],
                    postgresql_where=sa.text('finished_at is null'))


def downgrade() -> None:
    op.drop_index('ix_job_queue_due', table_name='job_queue')
    op.drop_index('ux_job_queue_waiting', table_name='job_queue')
    op.drop_table('job_queue')
