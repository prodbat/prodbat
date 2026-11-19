# Running Prodbat yourself

Prodbat is one container and a Postgres database. The container runs the web server and the worker (outside checks, problem detection, email) together.

## With Docker

```sh
git clone https://github.com/prodbat/prodbat && cd prodbat
cp .env.example .env        # set the password, your address and your mail settings
docker compose up -d
docker compose exec prodbat python -m prodbat_server add-site yourapp.com --email you@example.com
```

`add-site` prints three things: the script tag for your site, the private link to your page, and a secret key for your server and the command line.

Put HTTPS in front of port 8000 with a reverse proxy (Caddy, nginx, or your host's own), and set `PRODBAT_PROXY_HOPS` to the number of proxies in front of the server. If it is wrong, every visitor looks like the same person.

## Without Docker

Python 3.12 or later, [uv](https://docs.astral.sh/uv/) and Postgres:

```sh
cd server
uv sync --no-dev
export PRODBAT_DATABASE_URL=postgresql+psycopg://user:password@localhost/prodbat
uv run python -m prodbat_server run          # creates the tables, then serves on port 8000 and watches
```

`run` is `migrate`, `serve` and `worker` in one process. They can also be run separately; several workers can run at once.

## Settings

All settings are environment variables. `.env.example` lists them with what each one does.

| Variable | What it is |
|---|---|
| `PRODBAT_DATABASE_URL` | The Postgres database |
| `PRODBAT_PUBLIC_URL` | The address browsers reach this server at |
| `PRODBAT_PROXY_HOPS` | Reverse proxies in front of the server (0 if none) |
| `PRODBAT_ADMIN_TOKEN` | Lets `npx prodbat init --token` create projects over the network |
| `PRODBAT_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `PRODBAT_MAIL_FROM` | Outgoing email; without a host, emails are printed in the log |
| `PRODBAT_KEEP_DAYS` | Days of visits, errors and checks to keep (365) |
| `PRODBAT_LOG_KEEP_DAYS` | Days of log lines to keep (7). A single site can be given its own number in the `log_keep_days` column of `projects` |
| `PRODBAT_LOG_LINES_PER_MINUTE` | Log lines one site may send a minute before the rest are dropped (3000) |
| `PRODBAT_TWILIO_ACCOUNT_SID`, `_AUTH_TOKEN`, `_FROM` | Text messages when customers are affected; without them, texts aren't offered |
| `PRODBAT_TEXT_COUNTRIES` | Optional: country codes texts may go to, such as `+1`. Empty allows any country |
| `PRODBAT_SLACK_CLIENT_ID`, `_CLIENT_SECRET` | Alerts in Slack; without them, Slack isn't offered. See below |
| `PRODBAT_WATCHER_URL` | A URL requested once a minute, so something outside can notice if this server stops |

### Slack

To let your project pages offer "Add to Slack", create a Slack app once: [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From a manifest, pick your workspace, and paste this with your server's address in place of `https://prodbat.yourdomain.com`:

```yaml
display_information:
  name: Prodbat
  description: Alerts when your app breaks
features:
  bot_user:
    display_name: Prodbat
oauth_config:
  redirect_urls:
    - https://prodbat.yourdomain.com/slack/callback
  scopes:
    bot:
      - incoming-webhook
      - chat:write
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
```

Then Basic Information → App Credentials: put the Client ID in `PRODBAT_SLACK_CLIENT_ID` and the Client Secret in `PRODBAT_SLACK_CLIENT_SECRET`, and restart. The app stays private to your workspace, which is all a self-hosted server needs. Slack's access token is stored in the `projects` table, so treat database backups as secret.

## What you take on

- **Nobody watches the watcher.** If this server stops, nothing tells you. Point `PRODBAT_WATCHER_URL` at any service that alerts when it stops being called.
- **One location.** Your site is loaded from wherever this server runs, so a network problem near the server can look like your site being down. A failed load is tried a second time before it counts.
- **Email deliverability is yours.** Alerts are only as reliable as the SMTP provider you give it.
- **There is no sign-in.** The page link is the secret; anyone who has it can read the page. Use `add-site --new-key` to replace a leaked secret key.
- **Log storage.** Log lines are rows in the same Postgres database. They are the bulk of its size; how long to keep them is your call.
- **Backups.** Everything is in the one Postgres database.

## Updating

```sh
git pull && docker compose up -d --build
```

The container updates the tables when it starts.
