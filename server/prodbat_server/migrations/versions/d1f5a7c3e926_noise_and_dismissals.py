"""Browser errors keep the browser's name and whether they came from the site's own scripts; a mute says
who made it (the owner, or the agent dismissing a task), why, and how big the problem was.

Revision ID: d1f5a7c3e926
Revises: c6a2e9d4f1b8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1f5a7c3e926'
down_revision: Union[str, Sequence[str], None] = 'c6a2e9d4f1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('browser_errors', sa.Column('browser', sa.Text(), server_default='', nullable=False))
    op.add_column('browser_errors', sa.Column('origin', sa.Text(), server_default='site', nullable=False))
    # Errors already kept are judged the same way; there is no browser name for them.
    from prodbat_server.noise import ELSEWHERE, classify

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "select e.id, e.message, e.file, e.stack, p.domain from browser_errors e join projects p on p.id = e.project_id"))
    elsewhere = [r.id for r in rows if classify(r.message, r.file, r.stack, r.domain)[0] == ELSEWHERE]
    for start in range(0, len(elsewhere), 1000):
        bind.execute(sa.text("update browser_errors set origin = 'elsewhere' where id = any(:ids)"),
                     {"ids": elsewhere[start:start + 1000]})

    op.add_column('mutes', sa.Column('by', sa.Text(), server_default='owner', nullable=False))
    op.add_column('mutes', sa.Column('reason', sa.Text(), nullable=True))
    op.add_column('mutes', sa.Column('reach', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('mutes', 'reach')
    op.drop_column('mutes', 'reason')
    op.drop_column('mutes', 'by')
    op.drop_column('browser_errors', 'origin')
    op.drop_column('browser_errors', 'browser')
