"""when the site first forwarded a Stripe event, so the page can say Stripe is
connected before the first payment

Revision ID: a3e8c1f7b264
Revises: d1f5a7c3e926
Create Date: 2026-11-20 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3e8c1f7b264'
down_revision: Union[str, Sequence[str], None] = 'd1f5a7c3e926'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('stripe_seen_at', sa.DateTime(timezone=True), nullable=True))
    # A project with payments has plainly forwarded Stripe events.
    op.execute("update projects set stripe_seen_at = (select min(ts) from payments where project_id = projects.id) "
               "where exists (select 1 from payments where project_id = projects.id)")


def downgrade() -> None:
    op.drop_column('projects', 'stripe_seen_at')
