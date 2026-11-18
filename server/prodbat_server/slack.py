"""Alerts in a Slack channel, alongside email.

The owner presses "Add to Slack" on their page and picks a channel; Slack
sends them back with a code that is swapped for a bot token. Each problem
becomes one message, and everything later about it ("Broken" after "Needs a
look", "Working again") replies in that message's thread and is also shown
in the channel.

Text that strangers wrote (error messages, routes) is escaped, so it can't
mention @channel or hide a link.
"""

import datetime as dt
import secrets
import urllib.parse

import httpx
from sqlalchemy import select

from .config import Settings
from .models import Incident, Project, SlackPost

API = "https://slack.com/api"
SCOPES = "incoming-webhook,chat:write"
MAX_ATTEMPTS = 5
# Answers that mean trying again won't help; the owner has to connect again.
GONE = {
    "invalid_auth": "Slack no longer accepts our access. Connect it again.",
    "token_revoked": "Prodbat was removed from your Slack. Connect it again to get alerts there.",
    "account_inactive": "Prodbat was removed from your Slack. Connect it again to get alerts there.",
    "token_expired": "Slack no longer accepts our access. Connect it again.",
    "channel_not_found": "The Slack channel was deleted. Connect Slack again and pick another.",
    "is_archived": "The Slack channel was archived. Connect Slack again and pick another.",
    "missing_scope": "Slack no longer lets Prodbat post. Connect it again.",
}
COLOR = {"broken": "#C0392B", "look": "#D68910", "ok": "#1E8449", "test": "#5D6D7E"}


def can_post(project: Project, settings: Settings) -> bool:
    return bool(settings.slack_configured and project.slack_enabled and project.paused_at is None
                and project.slack_channel_id and project.slack_token and not project.slack_error)


def esc(value: str) -> str:
    """Slack reads <...> as mentions and links; & < > must be written as entities."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def redirect_uri(settings: Settings) -> str:
    return f"{settings.base}/slack/callback"


def authorize_url(project: Project, settings: Settings) -> str:
    """Where "Add to Slack" goes. The state names the project and carries a
    random value stored on it, checked when Slack sends the owner back."""
    project.slack_state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({"client_id": settings.slack_client_id, "scope": SCOPES,
                                    "redirect_uri": redirect_uri(settings),
                                    "state": f"{project.id.hex}.{project.slack_state}"})
    return f"https://slack.com/oauth/v2/authorize?{query}"


async def finish_connecting(http: httpx.AsyncClient, settings: Settings, project: Project, code: str) -> str | None:
    """Swap Slack's code for a token. Returns None when connected, or a sentence saying what went wrong."""
    response = await http.post(f"{API}/oauth.v2.access", data={
        "client_id": settings.slack_client_id, "client_secret": settings.slack_client_secret,
        "code": code, "redirect_uri": redirect_uri(settings)})
    answer = response.json() if response.status_code == 200 else {}
    hook = answer.get("incoming_webhook") or {}
    if not answer.get("ok") or not answer.get("access_token") or not hook.get("channel_id"):
        return "Slack didn't finish connecting. Try again."
    project.slack_token = answer["access_token"]
    project.slack_webhook_url = hook.get("url")
    project.slack_channel_id = hook["channel_id"]
    project.slack_channel = hook.get("channel") or "your channel"
    project.slack_team = (answer.get("team") or {}).get("name")
    project.slack_state = project.slack_error = None
    return None


async def disconnect(http: httpx.AsyncClient, project: Project) -> None:
    """Forget the connection, and tell Slack to drop the token (best effort)."""
    token = project.slack_token
    project.slack_token = project.slack_webhook_url = project.slack_channel_id = None
    project.slack_channel = project.slack_team = project.slack_state = project.slack_error = None
    if token:
        try:
            await http.post(f"{API}/auth.revoke", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            pass


# ---- what is posted -----------------------------------------------------------

def _post(db, project: Project, settings: Settings, text: str, color: str, body: str,
          incident: Incident | None = None) -> bool:
    if not can_post(project, settings):
        return False
    link = f"<{settings.base}/d/{project.page_token}|Open your Prodbat page>"
    blocks = [{"color": color, "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": link}]}]}]
    db.add(SlackPost(project_id=project.id, incident=incident, text=text[:300], blocks=blocks))
    return True


def post_alert(db, project: Project, incident: Incident, settings: Settings, prefix: str, todo: str,
               prompt: str, when: str) -> None:
    headline = f"{prefix}: {incident.title} on {project.domain}"
    body = "\n".join([
        f"*{esc(headline)}*",
        f"*What broke:* {esc(incident.what)}",
        f"*Since when:* {esc(when)}",
        f"*Who is affected:* {esc(incident.who)}",
        f"*What to do:* {esc(todo)}",
        "For your coding agent:",
        f"```{esc(prompt)}```",
    ])
    _post(db, project, settings, headline, COLOR[incident.level], body, incident)


def post_recovery(db, project: Project, incident: Incident, settings: Settings, recovery: str, duration: str) -> None:
    headline = f"Working again: {incident.title} on {project.domain}"
    _post(db, project, settings, headline, COLOR["ok"],
          f"*{esc(headline)}*\n{esc(recovery)}\nIt was broken for {esc(duration)}.", incident)


def post_digest(db, project: Project, settings: Settings, new: list[Incident], about: str) -> None:
    headline = f"Needs a look: {len(new)} things about {about} on {project.domain}"
    lines = [f"*{esc(headline)}*", "None of them is breaking your site right now."]
    lines += [f"• *{esc(i.title)}*. {esc(i.todo)}" for i in new]
    _post(db, project, settings, headline, COLOR["look"], "\n".join(lines))


def post_test(db, project: Project, settings: Settings) -> bool:
    headline = f"Test: this is what a Prodbat alert for {project.domain} looks like"
    return _post(db, project, settings, headline, COLOR["test"], "\n".join([
        f"*{esc(headline)}*", "This is a test. Nothing is wrong.",
        "*What broke:* POST /api/checkout answered with an error (500) for 4 visitors in the last 15 minutes.",
        "*Since when:* 14:10 UTC today.", "*Who is affected:* 4 visitors so far.",
        "*What to do:* Paste the line in the alert into your coding agent, or roll back your last deploy."]))


def post_connected(db, project: Project, settings: Settings) -> None:
    text = (f"Prodbat will post here when something on {project.domain} breaks or needs a look, "
            f"and reply in the same thread when it's working again.")
    _post(db, project, settings, text, COLOR["test"], esc(text))


# ---- sending ------------------------------------------------------------------

async def _send(http: httpx.AsyncClient, project: Project, post: SlackPost) -> str | None:
    """Post one message. Returns the error code Slack gave, or None."""
    payload = {"channel": project.slack_channel_id, "text": post.text, "attachments": post.blocks,
               "unfurl_links": False, "unfurl_media": False}
    thread = post.incident.slack_ts if post.incident is not None else None
    if thread:
        payload |= {"thread_ts": thread, "reply_broadcast": True}
    response = await http.post(f"{API}/chat.postMessage", json=payload,
                               headers={"Authorization": f"Bearer {project.slack_token}"})
    if response.status_code == 429:
        return "ratelimited"
    answer = response.json()
    if answer.get("ok"):
        if post.incident is not None and not post.incident.slack_ts:
            post.incident.slack_ts = answer.get("ts")
        return None
    error = answer.get("error", f"status {response.status_code}")
    if error == "not_in_channel" and project.slack_webhook_url:
        # The bot was taken out of the channel; the webhook still posts there, without threads.
        hooked = await http.post(project.slack_webhook_url, json={"text": post.text, "attachments": post.blocks})
        return None if hooked.status_code == 200 else f"webhook {hooked.status_code}"
    return error


async def send_pending_slack(sessions, settings: Settings, http: httpx.AsyncClient) -> int:
    if not settings.slack_configured:
        return 0
    sent = 0
    async with sessions() as db:
        posts = (await db.scalars(
            select(SlackPost).where(SlackPost.sent_at.is_(None), SlackPost.attempts < MAX_ATTEMPTS)
            .order_by(SlackPost.created_at, SlackPost.id).limit(50).with_for_update(skip_locked=True))).all()
        for post in posts:
            project = await db.get(Project, post.project_id)
            await db.refresh(post, ["incident"])
            if not project.slack_token or not project.slack_channel_id:
                post.attempts, post.last_error = MAX_ATTEMPTS, "Slack is not connected"
                continue
            post.attempts += 1
            try:
                error = await _send(http, project, post)
            except Exception as exc:  # keep the rest moving
                error = f"{type(exc).__name__}: {exc}"
            if error is None:
                post.sent_at, post.last_error = dt.datetime.now(dt.UTC), None
                sent += 1
            elif error == "ratelimited":
                post.attempts -= 1
                break
            else:
                post.last_error = error[:500]
                if error in GONE:
                    project.slack_error = GONE[error]
                    post.attempts = MAX_ATTEMPTS
        await db.commit()
    return sent
