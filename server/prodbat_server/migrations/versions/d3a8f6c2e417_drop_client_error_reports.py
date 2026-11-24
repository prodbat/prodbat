"""404s and other 4xx answers reported as server errors by older libraries are
removed, and the problems they opened are closed without a "working again",
since nothing was broken

Revision ID: d3a8f6c2e417
Revises: c2d7e4a9b153
Create Date: 2026-11-24 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd3a8f6c2e417'
down_revision: Union[str, Sequence[str], None] = 'c2d7e4a9b153'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# api.CLIENT_ERRORS as it was when this ran.
KINDS = [
    "Http404", "PermissionDenied", "SuspiciousOperation", "DisallowedHost", "DisallowedRedirect",
    "SuspiciousFileOperation", "SuspiciousMultipartForm", "RequestDataTooBig", "TooManyFieldsSent",
    "TooManyFilesSent", "BadRequest",
    "ActiveRecord::RecordNotFound", "ActionController::RoutingError", "AbstractController::ActionNotFound",
    "ActionController::UnknownFormat", "ActionController::InvalidAuthenticityToken",
    "ActionController::InvalidCrossOriginRequest", "ActionController::ParameterMissing",
    "ActionController::BadRequest", "ActionController::MethodNotAllowed", "ActionController::UnknownHttpMethod",
    "ActionDispatch::Http::Parameters::ParseError", "ActionDispatch::Http::MimeNegotiation::InvalidType",
]
# coalesce: a report without a status must count as a real error, never as unknown.
CLIENT = """coalesce(kind = any(:kinds) or status between 400 and 499
            or message like 'NEXT_NOT_FOUND%' or message like 'NEXT_REDIRECT%'
            or message like 'NEXT_HTTP_ERROR_FALLBACK%', false)"""


def upgrade() -> None:
    bind = op.get_bind()
    params = {"kinds": KINDS}
    # A problem made only of such reports is closed quietly: no email, nothing was broken.
    bind.execute(sa.text(f"""
        update incidents i set ended_at = now()
        where i.kind = 'server' and i.ended_at is null
          and exists (select 1 from server_errors e where e.project_id = i.project_id and e.fingerprint = i.key
                      and ({CLIENT}))
          and not exists (select 1 from server_errors e where e.project_id = i.project_id and e.fingerprint = i.key
                          and not ({CLIENT}))"""), params)
    bind.execute(sa.text(f"delete from server_errors where {CLIENT}"), params)


def downgrade() -> None:
    pass
