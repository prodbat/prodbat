"""per-project retention, check interval, pause, and setup progress

Revision ID: 7c2e1f0a9b31
Revises: 41b479b58772
Create Date: 2026-11-18 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '7c2e1f0a9b31'
down_revision: Union[str, Sequence[str], None] = '41b479b58772'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STAMPS = ["paused_at", "first_data_at", "first_server_at", "live_email_at", "nudge_email_at"]


def upgrade() -> None:
    op.add_column('projects', sa.Column('keep_days', sa.Integer(), nullable=True))
    op.add_column('projects', sa.Column('check_every_seconds', sa.Integer(), server_default='300', nullable=False))
    for name in STAMPS:
        op.add_column('projects', sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    # Projects that already exist have had data, and should not get the setup emails.
    op.alter_column('emails', 'project_id', nullable=True)
    op.execute("update projects set first_data_at = created_at, live_email_at = created_at, "
               "nudge_email_at = created_at where exists (select 1 from pageviews where project_id = projects.id)")


def downgrade() -> None:
    op.execute("delete from emails where project_id is null")
    op.alter_column('emails', 'project_id', nullable=False)
    for name in reversed(STAMPS):
        op.drop_column('projects', name)
    op.drop_column('projects', 'check_every_seconds')
    op.drop_column('projects', 'keep_days')
