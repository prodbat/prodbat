"""visits per hour for each site, the record its normal traffic is learned
from; filled from the last 5 weeks of page views

Revision ID: c2d7e4a9b153
Revises: b4f9d2a6c831
Create Date: 2026-11-23 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c2d7e4a9b153'
down_revision: Union[str, Sequence[str], None] = 'b4f9d2a6c831'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'visit_hours',
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('hour', sa.DateTime(timezone=True), primary_key=True),
        sa.Column('visits', sa.Integer(), nullable=False),
    )
    # A visit begins with a page view after the visitor was away 30 minutes (traffic.VISIT_GAP).
    op.execute("""
        insert into visit_hours (project_id, hour, visits)
        select project_id, date_trunc('hour', ts, 'UTC'), count(*) from (
            select project_id, ts, lag(ts) over (partition by project_id, visitor order by ts) prev from pageviews
            where ts >= now() - interval '35 days' - interval '30 minutes') v
        where ts >= now() - interval '35 days' and ts < date_trunc('hour', now(), 'UTC')
          and (prev is null or ts - prev > interval '30 minutes')
        group by 1, 2""")


def downgrade() -> None:
    op.drop_table('visit_hours')
