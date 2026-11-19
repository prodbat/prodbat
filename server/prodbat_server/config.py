from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from PRODBAT_* environment variables (and .env if present)."""

    model_config = SettingsConfigDict(env_prefix="PRODBAT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/prodbat_dev"
    # Where this server is reachable from a browser; used in script tags,
    # links in emails and what the CLI prints.
    public_url: str = "http://localhost:8000"
    # Number of reverse proxies in front of the server that append to
    # X-Forwarded-For. 0 means use the socket address.
    proxy_hops: int = 0

    # Whoever holds this can create projects (`prodbat init --token`).
    # Without it, projects can only be created on the server itself with
    # `python -m prodbat_server add-site`.
    admin_token: str | None = None

    # Outgoing email. Without smtp_host, emails are printed, not sent.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    mail_from: str = "alerts@localhost"

    # Text messages through Twilio. Without these, no texts are sent and the
    # page doesn't offer them.
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # The number texts come from (+15551234567), or a Messaging Service id (MG...).
    twilio_from: str | None = None
    # Country codes texts may go to, comma separated ("+1" for the US). Empty:
    # any country. Texting abroad costs more and needs its own registration.
    text_countries: str = ""

    @property
    def text_prefixes(self) -> list[str]:
        return [p.strip() for p in self.text_countries.split(",") if p.strip()]

    @property
    def texts_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)

    # A Slack app, so owners can have alerts posted to a channel too. Without
    # these the page doesn't offer Slack. docs/self-hosting.md has the app's manifest.
    slack_client_id: str | None = None
    slack_client_secret: str | None = None

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    # Notifications on the owner's phone or computer through the browser's own
    # push service (web push). Without these the page doesn't offer them. Make a
    # pair with `python -m prodbat_server vapid-keys`; the subject is a mailto: or
    # https: address push services can contact about this server.
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str | None = None

    @property
    def push_configured(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key and self.vapid_subject)

    # A Telegram bot, so owners can have alerts sent to a Telegram chat or group.
    # From @BotFather: the token, and the bot's username without the @. The secret
    # is any long random string; Telegram sends it back with every update. Without
    # these the page doesn't offer Telegram. docs/self-hosting.md has the steps.
    telegram_bot_token: str | None = None
    telegram_bot_name: str | None = None
    telegram_webhook_secret: str | None = None

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_bot_name and self.telegram_webhook_secret)

    # A GitHub App, so owners can have a GitHub issue opened for each problem,
    # for a coding agent that works from issues. This is not GitHub sign-in (the
    # hosted service's PRODBAT_GITHUB_CLIENT_ID is a separate OAuth app). The
    # private key is the PEM GitHub gives you; in one line, write its line breaks
    # as \n. Without these the page doesn't offer GitHub. docs/self-hosting.md
    # has the steps.
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_app_private_key: str | None = None

    @property
    def github_configured(self) -> bool:
        return bool(self.github_app_id and self.github_app_slug and self.github_app_private_key)

    @property
    def github_private_key_pem(self) -> bytes:
        return (self.github_app_private_key or "").strip().replace("\\n", "\n").encode()

    # Signed webhooks need no settings; this turns them off for a whole server.
    webhooks: bool = True

    # Where this process checks sites from, such as "oregon". Probes in other
    # regions (`python -m prodbat_server probe`) record checks under their own name.
    region: str = ""

    # How long visits, errors and checks are kept.
    keep_days: int = 365
    # How long log lines are kept, unless a project has its own number.
    log_keep_days: int = 7
    # Log lines accepted per project per minute; the rest are dropped.
    log_lines_per_minute: int = 3000

    # If set, the worker requests this URL once a minute, so an outside
    # watcher can tell you when this server itself has stopped.
    watcher_url: str | None = None

    # Python modules that add to this server, comma-separated. Each has
    # setup(app), and may have `async def tick(sessions, settings, http)`
    # for the worker to call on every pass.
    extensions: str = ""

    @field_validator("database_url")
    @classmethod
    def _psycopg(cls, url: str) -> str:
        """Hosts such as Render hand out postgres:// or postgresql:// addresses."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def extension_modules(self) -> list[str]:
        return [name.strip() for name in self.extensions.split(",") if name.strip()]

    @property
    def base(self) -> str:
        return self.public_url.rstrip("/")
