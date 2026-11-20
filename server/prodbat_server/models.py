import datetime as dt
import hashlib
import secrets
import uuid

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Identity, Index, LargeBinary, Text, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def new_site_key() -> str:
    return "pb_" + secrets.token_urlsafe(9)


def new_page_token() -> str:
    return secrets.token_urlsafe(24)


def new_secret() -> str:
    return "pbs_" + secrets.token_urlsafe(24)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def normalize_domain(host: str) -> str:
    host = host.strip().lower()
    host = host.split("://", 1)[-1].split("/", 1)[0]
    return host.removeprefix("www.")


def pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def project_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))


def timestamp(**kw) -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Public: it is printed in the script tag on the site.
    key: Mapped[str] = mapped_column(Text, unique=True, default=new_site_key)
    # Secret: whoever has it can read this project's page at /d/<token>.
    # Stands in for sign-in, which the open-source server does not have.
    page_token: Mapped[str] = mapped_column(Text, unique=True, default=new_page_token)
    # sha256 of the secret key the site's own server and the CLI use.
    # The key itself is shown once, when the project is created.
    secret_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    # Lowercase, without a leading "www.".
    domain: Mapped[str] = mapped_column(Text, unique=True)
    alert_email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    # When the worker should next load the site from outside.
    next_check_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    # Looked up about once a day.
    cert_expires_at: Mapped[dt.datetime | None] = timestamp()
    domain_expires_at: Mapped[dt.datetime | None] = timestamp()
    expiry_checked_at: Mapped[dt.datetime | None] = timestamp()
    advisories_checked_at: Mapped[dt.datetime | None] = timestamp()
    weekly_sent_at: Mapped[dt.datetime | None] = timestamp()
    freshness_checked_at: Mapped[dt.datetime | None] = timestamp()
    # When `npx prodbat deps` last sent the package list.
    dependencies_sent_at: Mapped[dt.datetime | None] = timestamp()
    # Routes the owner has named as part of paying, signing in or signing
    # up, on top of the ones recognised by name: {"pay": ["/api/buy"]}.
    flows: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Pages besides the home page to load from outside: ["/login"].
    pages: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # Days to keep log lines; null means the server's default.
    log_keep_days: Mapped[int | None] = mapped_column()
    # Days to keep everything else; null means the server's default.
    keep_days: Mapped[int | None] = mapped_column()
    # Seconds between loads of the site from outside.
    check_every_seconds: Mapped[int] = mapped_column(default=300, server_default="300")
    # Set while monitoring is switched off (a hosted trial that ended, or
    # an owner who paused it). Nothing is checked, stored or emailed.
    paused_at: Mapped[dt.datetime | None] = timestamp()
    # The first page view from the site, and the first report from its
    # own server: what the page's setup checklist ticks off.
    first_data_at: Mapped[dt.datetime | None] = timestamp()
    first_server_at: Mapped[dt.datetime | None] = timestamp()
    # The first Stripe event the site forwarded, of any kind: Stripe is connected
    # even when nobody has paid yet.
    stripe_seen_at: Mapped[dt.datetime | None] = timestamp()
    # The agent's setup checklist (setup.py): suggestion ids the agent said do
    # not apply to this app, and items it has finished that wait for their
    # first data, as {id: when it said so}.
    setup_skipped: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    setup_waiting: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # One-off emails, so each goes once.
    live_email_at: Mapped[dt.datetime | None] = timestamp()
    nudge_email_at: Mapped[dt.datetime | None] = timestamp()
    # Text messages when customers are affected: a confirmed number, and a
    # number waiting for its code. A hosted plan can switch texts off.
    sms_to: Mapped[str | None] = mapped_column(Text)
    sms_pending: Mapped[str | None] = mapped_column(Text)
    sms_code_hash: Mapped[str | None] = mapped_column(Text)
    sms_code_at: Mapped[dt.datetime | None] = timestamp()
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Also check from the other regions' probes, and call the site down only when
    # they agree. A hosted plan switches this on.
    check_regions: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Show which source each payment came from. A hosted plan can switch it off.
    attribution_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Events from the script accepted per day; the rest are dropped. Empty means no cap.
    daily_event_cap: Mapped[int | None] = mapped_column()
    # The owner's time zone ("Europe/London"), from their browser: times in emails, and
    # when the Monday email goes. Empty means UTC.
    timezone: Mapped[str | None] = mapped_column(Text)
    # Alerts in a Slack channel too. The bot token is what posts; the webhook is the
    # fallback if the bot is ever taken out of the channel. A hosted plan can switch it off.
    slack_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    slack_token: Mapped[str | None] = mapped_column(Text)
    slack_webhook_url: Mapped[str | None] = mapped_column(Text)
    slack_channel_id: Mapped[str | None] = mapped_column(Text)
    slack_channel: Mapped[str | None] = mapped_column(Text)  # "#alerts"
    slack_team: Mapped[str | None] = mapped_column(Text)
    # Random value sent to Slack when connecting, checked when Slack sends the owner back.
    slack_state: Mapped[str | None] = mapped_column(Text)
    # Why Slack stopped taking messages (the channel was archived, the app was removed).
    slack_error: Mapped[str | None] = mapped_column(Text)
    # Notifications on the owner's devices through web push (see PushSubscription).
    # A hosted plan can switch them off. The error says why a device was dropped.
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    push_error: Mapped[str | None] = mapped_column(Text)
    # Alerts in a Telegram chat or group. The code is what the owner's "Start" link
    # carries to the bot, good for an hour. A hosted plan can switch it off.
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat: Mapped[str | None] = mapped_column(Text)  # "Ann Lee" or the group's title
    telegram_code: Mapped[str | None] = mapped_column(Text, unique=True)
    telegram_code_at: Mapped[dt.datetime | None] = timestamp()
    # Why Telegram stopped taking messages (the bot was blocked or removed from the group).
    telegram_error: Mapped[str | None] = mapped_column(Text)
    # A signed webhook: each problem, update and fix check is POSTed as JSON to the
    # owner's HTTPS address, signed with the secret (kept, because signing needs it).
    # A hosted plan can switch it off. The error says why it was turned off.
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret: Mapped[str | None] = mapped_column(Text)
    webhook_error: Mapped[str | None] = mapped_column(Text)
    # Since when every attempt has failed, and how many answers in a row were 410 Gone.
    webhook_failing_since: Mapped[dt.datetime | None] = timestamp()
    webhook_gone: Mapped[int] = mapped_column(default=0, server_default="0")
    # GitHub issues through Prodbat's GitHub App: the installation the owner made,
    # the repositories it can see ({"name": "ann/shop", "private": true}), and the
    # one they picked. A hosted plan can switch it off.
    github_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_account: Mapped[str | None] = mapped_column(Text)  # the user or organisation it is installed on
    github_repos: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    github_repo: Mapped[str | None] = mapped_column(Text)  # "ann/shop"
    # Random value sent to GitHub when connecting, checked when GitHub sends the owner back.
    github_state: Mapped[str | None] = mapped_column(Text)
    github_state_at: Mapped[dt.datetime | None] = timestamp()
    # The GitHub login that connected it, as GitHub told us after "Authorize Prodbat".
    github_login: Mapped[str | None] = mapped_column(Text)
    # For ten minutes after GitHub sends the owner back: what their GitHub account
    # can reach ({"nonce", "at", "login", "installations": [{"id", "account",
    # "repos"}]}), so they can pick a repository. Never a token.
    github_pending: Mapped[dict | None] = mapped_column(JSON)
    # Also open issues for "Needs a look", not only "Broken".
    github_look: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The owner's own line at the end of each issue ("@claude please fix this"), and
    # GitHub logins to assign ("Copilot, ann").
    github_mention: Mapped[str | None] = mapped_column(Text)
    github_assignees: Mapped[str | None] = mapped_column(Text)
    # Why issues stopped (the app was uninstalled, the repository is out of reach).
    github_error: Mapped[str | None] = mapped_column(Text)
    # Something GitHub refused that we worked around (an assignee it wouldn't take).
    github_note: Mapped[str | None] = mapped_column(Text)


class DailySalt(Base):
    """One random salt per UTC day. Older rows are deleted, so a visitor
    hash cannot be recomputed or linked across days."""

    __tablename__ = "daily_salts"

    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    salt: Mapped[bytes] = mapped_column(LargeBinary)


class Pageview(Base):
    __tablename__ = "pageviews"
    __table_args__ = (Index("ix_pageviews_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    visitor: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    # Display name ("Google", "ChatGPT", "example.org"); null for direct and internal.
    source: Mapped[str | None] = mapped_column(Text)
    # ai, search, social, referral, campaign, direct or internal.
    source_kind: Mapped[str] = mapped_column(Text)
    # A short reference the site can pass to Stripe so a payment can be
    # traced back to this visit's source.
    ref: Mapped[str | None] = mapped_column(Text)


class BrowserError(Base):
    __tablename__ = "browser_errors"
    __table_args__ = (Index("ix_browser_errors_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    visitor: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    # Same message, file and line give the same fingerprint.
    fingerprint: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    file: Mapped[str] = mapped_column(Text)
    line: Mapped[int] = mapped_column()
    col: Mapped[int] = mapped_column()
    stack: Mapped[str] = mapped_column(Text)
    # "Safari 18", "Instagram's built-in browser" (noise.browser_name). Never the whole User-Agent.
    browser: Mapped[str] = mapped_column(Text, default="", server_default="")
    # "site" when the trace runs through the site's own scripts, "elsewhere" when it doesn't (noise.py).
    # Only the site's own errors are alerts.
    origin: Mapped[str] = mapped_column(Text, default="site", server_default="site")


class FailedRequest(Base):
    """A request from the page to the site's own server that returned a
    5xx, or never got an answer (status 0)."""

    __tablename__ = "failed_requests"
    __table_args__ = (Index("ix_failed_requests_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    visitor: Mapped[str] = mapped_column(Text)
    # The page the visitor was on.
    path: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    # The path that was requested.
    route: Mapped[str] = mapped_column(Text)
    status: Mapped[int] = mapped_column()


class ServerError(Base):
    """An exception reported by the site's own server, through the library."""

    __tablename__ = "server_errors"
    __table_args__ = (Index("ix_server_errors_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    # Exception type and the deepest frame in the app's own code.
    fingerprint: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # "KeyError"
    message: Mapped[str] = mapped_column(Text)
    # "POST /api/checkout", or the job name; empty when unknown.
    route: Mapped[str] = mapped_column(Text)
    # "billing/views.py:88 in create_session"
    where: Mapped[str] = mapped_column(Text)
    trace: Mapped[str] = mapped_column(Text)
    # The response status, when the report is of a 5xx answer.
    status: Mapped[int | None] = mapped_column()


class SiteCheck(Base):
    """One load of the site's home page from outside."""

    __tablename__ = "site_checks"
    __table_args__ = (Index("ix_site_checks_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    path: Mapped[str] = mapped_column(Text, default="/", server_default="/")
    # Null when the site answered but would not let us in, so we cannot tell.
    ok: Mapped[bool | None] = mapped_column()
    status: Mapped[int | None] = mapped_column()
    ms: Mapped[int | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    # Where it was checked from; empty for a server that checks from one place.
    region: Mapped[str | None] = mapped_column(Text)


class Incident(Base):
    """A problem. Open while ended_at is null; at most one open per
    (project, kind, key).

    kind: down (key ""), route ("POST /api/checkout"), error (browser
    error fingerprint), server (server error fingerprint), job (job name),
    cert, domain, advisory ("npm/lodash").
    level: broken (customers are affected now; emailed at once) or look.
    """

    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_project_started", "project_id", "started_at"),
        Index("ux_incidents_open", "project_id", "kind", "key", unique=True, postgresql_where=text("ended_at is null")),
    )

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    kind: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = timestamp()
    ended_at: Mapped[dt.datetime | None] = timestamp()
    # "POST /api/checkout is failing" (no full stop: it goes in subjects).
    title: Mapped[str] = mapped_column(Text)
    # One or two sentences of evidence.
    what: Mapped[str] = mapped_column(Text)
    # "4 visitors so far."
    who: Mapped[str] = mapped_column(Text)
    # What to do about it; a sentence for the owner or their coding agent.
    todo: Mapped[str] = mapped_column(Text, default="")
    # The facts behind it as separate values (a route, a package and its
    # versions), so a task for a coding agent can be written from fixed
    # text and checked values instead of from sentences.
    data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Message-ID of the first email, so later ones land in the same thread.
    message_id: Mapped[str | None] = mapped_column(Text)
    # The first Slack message's timestamp, so later ones reply in its thread.
    slack_ts: Mapped[str | None] = mapped_column(Text)
    # The first Telegram message's id, so later ones reply to it.
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # The GitHub issue opened for it, in the project's repository, and a hash of
    # the last comment posted there, so the same words are never posted twice in a row.
    github_issue: Mapped[int | None] = mapped_column(BigInteger)
    github_said: Mapped[str | None] = mapped_column(Text)


class Mute(Base):
    """The owner said "this is fine, stop telling me", or the coding agent
    dismissed a task as not the site's problem. The owner's mute is for good.
    The agent's lasts while the problem stays the size it was: `reach` is how
    many visitors (or server errors) it had, and it comes back if it grows to
    twice that and at least DISMISS_REGROW, or if it starts breaking the site."""

    __tablename__ = "mutes"
    __table_args__ = (UniqueConstraint("project_id", "kind", "key"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    kind: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    by: Mapped[str] = mapped_column(Text, default="owner", server_default="owner")  # "owner" or "agent"
    reason: Mapped[str | None] = mapped_column(Text)
    reach: Mapped[int | None] = mapped_column()
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())


class Email(Base):
    """Outbox. Written in the same transaction as the change that caused
    it; the worker sends whatever has no sent_at."""

    __tablename__ = "emails"
    __table_args__ = (Index("ix_emails_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    # Empty for email about no one project (an account, a sign-in code).
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    to: Mapped[str] = mapped_column(Text)
    sender_name: Mapped[str] = mapped_column(Text, default="Prodbat")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Attribution(Base):
    """Where the buyer of a subscription came from, found at its checkout."""

    __tablename__ = "attributions"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    subscription: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())


class TextMessage(Base):
    """Outbox for text messages, sent by the worker like emails."""

    __tablename__ = "texts"
    __table_args__ = (Index("ix_texts_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    to: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class SlackPost(Base):
    """Outbox for Slack messages, sent by the worker like emails. One about a
    problem starts a thread, and later ones about it reply in that thread."""

    __tablename__ = "slack_posts"
    __table_args__ = (Index("ix_slack_posts_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    incident: Mapped["Incident | None"] = relationship()
    text: Mapped[str] = mapped_column(Text)  # what notifications show
    blocks: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class TelegramMessage(Base):
    """Outbox for Telegram messages, sent by the worker like emails. One about a
    problem is sent first, and later ones about it reply to that one."""

    __tablename__ = "telegram_messages"
    __table_args__ = (Index("ix_telegram_messages_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    incident: Mapped["Incident | None"] = relationship()
    text: Mapped[str] = mapped_column(Text)  # Telegram's HTML, already escaped
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class PushSubscription(Base):
    """One browser or phone that shows the project's alerts as notifications.
    The endpoint is the push service's address for it; the keys encrypt what
    is sent, so the push service can't read it."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)  # "Chrome on Android"
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    last_used_at: Mapped[dt.datetime | None] = timestamp()


class PushMessage(Base):
    """Outbox for notifications, sent by the worker to every device of the
    project, or to one device when subscription_id is set."""

    __tablename__ = "push_messages"
    __table_args__ = (Index("ix_push_messages_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    incident: Mapped["Incident | None"] = relationship()
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("push_subscriptions.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class WebhookDelivery(Base):
    """Outbox for the project's webhook. The body is written on the first
    attempt and kept, so every retry sends the same JSON with the same id."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_due", "next_attempt_at", postgresql_where=text("sent_at is null and gave_up_at is null")),
        Index("ix_webhook_deliveries_project", "project_id", "created_at"),
    )

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    incident: Mapped["Incident | None"] = relationship()
    # "incident.opened", "task.verified", "test"...
    event: Mapped[str] = mapped_column(Text)
    # The Prodbat-Delivery header, for the receiver to drop repeats.
    delivery_id: Mapped[str] = mapped_column(Text, unique=True)
    # What the event adds to the incident: the recovery sentence, a fix check's answer.
    data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    next_attempt_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    last_attempt_at: Mapped[dt.datetime | None] = timestamp()
    sent_at: Mapped[dt.datetime | None] = timestamp()
    gave_up_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    # The last answer's status code (null when there was no answer), and what went wrong.
    last_status: Mapped[int | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)


class GitHubPost(Base):
    """Outbox for GitHub: open an issue (or comment on the one already open), comment,
    close, or a test issue. Opening is written at send time, with the evidence then."""

    __tablename__ = "github_posts"
    __table_args__ = (Index("ix_github_posts_unsent", "created_at", postgresql_where=text("sent_at is null")),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))
    incident: Mapped["Incident | None"] = relationship()
    action: Mapped[str] = mapped_column(Text)  # alert, comment, close, test
    text: Mapped[str] = mapped_column(Text, default="")  # Markdown for a comment, already escaped
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = timestamp()
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("project_id", "stripe_id"),
        Index("ix_payments_project_ts", "project_id", "ts"),
    )

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp()
    # The Stripe object the payment came from (invoice or checkout session).
    stripe_id: Mapped[str] = mapped_column(Text)
    # In the currency's smallest unit, as Stripe reports it.
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text)
    product: Mapped[str] = mapped_column(Text)
    # Where the buyer first came from, when the site passed a reference.
    source: Mapped[str | None] = mapped_column(Text)
    # The Stripe subscription an invoice payment belongs to, so the source found
    # at its checkout can be credited to every renewal.
    subscription: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """Something the site does on a schedule. It checks in when it
    finishes; if it is late, that is a problem."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    name: Mapped[str] = mapped_column(Text)
    every_seconds: Mapped[int] = mapped_column()
    last_ping_at: Mapped[dt.datetime] = timestamp()
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())


class Dependency(Base):
    """A package the site runs in production, from its lockfile. Names
    and versions only; no source code."""

    __tablename__ = "dependencies"
    __table_args__ = (UniqueConstraint("project_id", "ecosystem", "name"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    # npm, PyPI, RubyGems, Packagist, Go, crates.io, or "runtime" for the
    # language itself (name "node", "python").
    ecosystem: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    # Named in the project's own manifest, not pulled in by something else.
    direct: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The newest released version: of the package for direct dependencies,
    # of the same release line for a language.
    latest: Mapped[str | None] = mapped_column(Text)
    # What its authors say if they have deprecated or withdrawn this version.
    deprecated: Mapped[str | None] = mapped_column(Text)
    # Known security problems that are not rated serious or have no fix yet.
    issues: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # When the registry was last asked about this exact version.
    facts_checked_at: Mapped[dt.datetime | None] = timestamp()


class PackageFacts(Base):
    """What the public registries say about one version of a package,
    shared by every site that uses it, so it is asked about once. Each
    group of facts has its own time it was last asked (facts.py says how
    long each one is good for)."""

    __tablename__ = "package_facts"
    __table_args__ = (UniqueConstraint("ecosystem", "name", "version"),)

    id: Mapped[int] = pk()
    ecosystem: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    # From OSV. Serious: rated high or critical with a fix, as
    # [{"id", "summary", "fixed"}]. Issues: the rest, as sentences.
    serious: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    issues: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    advisories_checked_at: Mapped[dt.datetime | None] = timestamp()
    # From deps.dev: the newest release of the package.
    latest: Mapped[str | None] = mapped_column(Text)
    latest_checked_at: Mapped[dt.datetime | None] = timestamp()
    # From deps.dev: what its authors say if they deprecated this version.
    deprecated: Mapped[str | None] = mapped_column(Text)
    deprecated_checked_at: Mapped[dt.datetime | None] = timestamp()


class EolFacts(Base):
    """A product's release lines from endoflife.date (or Node.js's own
    list of releases), shared by every site that runs it."""

    __tablename__ = "eol_facts"

    product: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[list] = mapped_column(JSON)
    checked_at: Mapped[dt.datetime] = timestamp()


class QueueReport(Base):
    """A snapshot of one job queue, sent by the site's own server about
    once a minute."""

    __tablename__ = "queue_reports"
    __table_args__ = (Index("ix_queue_reports_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    system: Mapped[str] = mapped_column(Text)  # celery, rq, bullmq, sidekiq, custom
    name: Mapped[str] = mapped_column(Text)
    # Jobs waiting to start, and how long the oldest has waited. Null
    # when the integration cannot see the queue itself.
    waiting: Mapped[int | None] = mapped_column()
    oldest_seconds: Mapped[int | None] = mapped_column()
    # Since the previous report: jobs finished, jobs failed, and the
    # longest any job waited before it started.
    done: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    max_wait_seconds: Mapped[int | None] = mapped_column()


class LogLine(Base):
    __tablename__ = "log_lines"
    __table_args__ = (Index("ix_log_lines_project_ts", "project_id", "ts"),)

    id: Mapped[int] = pk()
    project_id: Mapped[uuid.UUID] = project_fk()
    ts: Mapped[dt.datetime] = timestamp(server_default=func.now())
    level: Mapped[str] = mapped_column(Text)  # debug, info, warning, error
    message: Mapped[str] = mapped_column(Text)
    fields: Mapped[dict | None] = mapped_column(JSON)


class QueuedJob(Base):
    """Work for the worker to do soon, such as checking a project's
    packages after the list changes. (The `jobs` table is the site's own
    scheduled jobs.)"""

    __tablename__ = "job_queue"
    __table_args__ = (
        # One waiting job per kind and key: enqueueing it again is a no-op. A
        # job that has started no longer counts, so a change that arrives
        # while it runs gets a run of its own.
        Index("ux_job_queue_waiting", "kind", "key", unique=True,
              postgresql_where=text("finished_at is null and locked_at is null")),
        Index("ix_job_queue_due", "run_after", postgresql_where=text("finished_at is null")),
    )

    id: Mapped[int] = pk()
    kind: Mapped[str] = mapped_column(Text)
    # What the job is about, to tell two jobs of a kind apart: the
    # project's id for a project's jobs.
    key: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Null for jobs that are not about one project.
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    run_after: Mapped[dt.datetime] = timestamp(server_default=func.now())
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # Set while a worker runs it. A lock older than the timeout means that worker died.
    locked_at: Mapped[dt.datetime | None] = timestamp()
    created_at: Mapped[dt.datetime] = timestamp(server_default=func.now())
    # Set when it succeeds or is given up on; `failed` says which.
    finished_at: Mapped[dt.datetime | None] = timestamp()
    failed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
