"""The project's pages: what there is to do, the site this week, and where alerts go.

There is no sign-in in the open-source server; the link is the secret.
"""

import datetime as dt
import hmac
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import github, push, slack, telegram, webhooks
from .api import queue_test_alert, script_tag, test_destinations
from .incidents import agent_prompt, resolve
from .models import Email, Incident, Mute, Project, PushSubscription, SiteCheck, hash_secret, new_secret
from .setup import checklist as setup_checklist
from .summary import gather
from .texts import can_text, confirm, normalize_number, start_confirming
from .words import ago, local_zone

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
PRIVATE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}


async def project_by_token(request: Request, db, token: str) -> Project:
    project = await db.scalar(select(Project).where(Project.page_token == token))
    if project is None:
        raise HTTPException(404, "no such page")
    guard = request.app.state.page_guard
    if guard is not None:
        await guard(request, project)  # raises to refuse, for example by redirecting to sign-in
    return project



def text_where(settings) -> str:
    """Which numbers can be texted, in words: "US numbers (+1)"."""
    prefixes = settings.text_prefixes
    if prefixes == ["+1"]:
        return "US numbers (+1)"
    return "numbers starting " + " or ".join(prefixes)

@router.get("/d/sw.js")
async def service_worker() -> FileResponse:
    """Shows notifications. At /d/ so a project's page can make itself its scope."""
    return FileResponse(Path(__file__).parent / "static" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


# Kinds of problem that are about the site misbehaving, for the seven days strip.
SITE_TROUBLE_SKIPS = ("advisory", "cert", "domain")


async def page_context(request: Request, token: str, sent: str | None) -> dict:
    """What the to-do page, the week page and the settings page all draw on."""
    settings = request.app.state.settings
    now = dt.datetime.now(dt.UTC)
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        devices = await push.devices(db, project, settings)
        deliveries = await webhooks.recent(db, project) if project.webhook_url else []
        places = await test_destinations(db, project, settings) if sent == "test" else ""
        summary = await gather(db, project, now)
        broken = next((p for p in summary.problems if p.level == "broken" and p.id), None)
        # A prompt for the coding agent for every open problem, each naming its own task.
        prompts = {p.id: agent_prompt(project, await db.get(Incident, p.id)) for p in summary.problems if p.id}
        prompt = prompts.get(broken.id) if broken else None
        emailed = await db.scalar(select(Email.id).where(Email.project_id == project.id).limit(1)) is not None
        checks = await db.scalar(select(func.count()).select_from(SiteCheck)
                                 .where(SiteCheck.project_id == project.id)) or 0
        last_fixed = await db.scalar(select(func.max(Incident.ended_at)).where(Incident.project_id == project.id))
        items = {item.id: item for item in await setup_checklist(db, project)}
        done = await done_this_week(db, project, now)
        days = await seven_days(db, project, now)
    notice = {"test": f"A test alert is on its way to {places}.",
              "muted": "Done. We will not tell you about that again.",
              "smscode": "We texted you a code. Enter it below to turn texts on.",
              "smson": "Texts are on. You'll get one when customers are affected.",
              "smswrong": "That code didn't match, or it ran out. Codes last 10 minutes.",
              "badnumber": "Write the number with its country code, like +1 555 123 4567.",
              "smsoff": "Texts are off.",
              "textcountry": f"Texts can only go to {text_where(settings)} for now. Alerts still come by email "
                             "and in Slack.",
              "slackon": f"Slack is connected. We posted a hello in {project.slack_channel}.",
              "slackno": "Slack wasn't connected. Nothing changed.",
              "slackoff": "Slack is disconnected. Alerts still go by email.",
              "pushon": "Notifications are on for this device. Send a test alert to see one.",
              "pushoff": "That device won't get notifications any more.",
              "telegramoff": "Telegram is disconnected. Alerts still go by email.",
              "webhookoff": "The webhook is removed. Nothing more is sent to it.",
              "webhooktest": "A test delivery is on its way to your webhook.",
              "webhookbad": "That address can't be used. It has to start with https:// and be reachable from the "
                            "internet, not a private network.",
              "githubon": "GitHub is connected." + (f" Issues will open in {project.github_repo}." if project.github_repo
                                                    else " Pick the repository for issues below."),
              "githubno": "GitHub wasn't connected. Nothing changed.",
              "githubpick": "GitHub showed us what your account can reach. Pick the repository for issues below.",
              "githubsaved": "Saved.",
              "githubtest": f"A test issue is on its way to {project.github_repo}. It is closed straight away.",
              "githuboff": "GitHub is disconnected. To remove the app from GitHub too, open your GitHub settings, "
                           "then Applications."}.get(sent or "")
    setup = setup_steps(project, summary, emailed, items)
    fix_now = [p for p in summary.problems if p.level == "broken"]
    this_week = [p for p in summary.problems if p.level == "look"]
    return {
        "page": summary, "token": token, "script_tag": script_tag(settings, project), "broken": broken,
        "prompt": prompt, "prompts": prompts, "alert_email": project.alert_email, "notice": notice, "setup": setup,
        "unfinished": [s for s in setup if not s["done"] and s.get("later") and not s.get("waiting")],
        "waiting_steps": [s for s in setup if s.get("waiting")],
        "fix_now": fix_now, "this_week": this_week, "seen": [p for p in summary.problems if p.level == "quiet"],
        "headline": headline(len(fix_now), len(this_week)), "done": done, "days": days,
        "paused": project.paused_at is not None, "checks": checks,
        "since": f"{project.created_at:%-d %b}", "last_fixed": ago(last_fixed, now) if last_fixed else None, "stalled": stalled(project),
        "texts": can_text(project, settings), "text_where": text_where(settings) if settings.text_prefixes else None, "sms_to": project.sms_to, "sms_pending": project.sms_pending,
        "needs_timezone": not project.timezone,
        "slack": settings.slack_configured and project.slack_enabled, "slack_channel": project.slack_channel,
        "slack_connected": bool(project.slack_token), "slack_team": project.slack_team,
        "slack_error": project.slack_error,
        "push": push.can_push(project, settings), "devices": devices, "push_error": project.push_error,
        "vapid_key": settings.vapid_public_key,
        "telegram": telegram.can_connect(project, settings), "telegram_chat": project.telegram_chat,
        "telegram_connected": bool(project.telegram_chat_id), "telegram_error": project.telegram_error,
        "telegram_links": telegram.links(project, settings) if telegram.can_connect(project, settings) else None,
        "webhook": webhooks.can_add(project, settings), "webhook_url": project.webhook_url,
        "webhook_error": project.webhook_error, "deliveries": deliveries,
        "github": github.can_connect(project, settings), "gh": project, "github_choices": github.choices(project),
        "github_issues_on": github.can_post(project, settings),
        "can_test": bool(project.alert_email) or slack.can_post(project, settings) or bool(devices)
                    or telegram.can_post(project, settings) or webhooks.can_post(project, settings)
                    or github.can_post(project, settings),
    }


def headline(fix_now: int, this_week: int) -> str:
    """The to-do page's first line: how much there is to do, and whether any of it is urgent."""
    if fix_now:
        return f"{'One thing' if fix_now == 1 else f'{fix_now} things'} to fix now."
    if this_week:
        return f"{'One thing' if this_week == 1 else f'{this_week} things'} to do. Nothing urgent."
    return "Nothing to do."


async def done_this_week(db, project: Project, now: dt.datetime) -> list[dict]:
    """Problems that closed in the last seven days, and how: fixed, waved off by the owner, or
    dismissed by the coding agent as not the site's own."""
    closed = (await db.scalars(select(Incident).where(
        Incident.project_id == project.id, Incident.ended_at >= now - dt.timedelta(days=7))
        .order_by(Incident.ended_at.desc()).limit(50))).all()
    waved = {(m.kind, m.key) for m in await db.scalars(select(Mute).where(Mute.project_id == project.id))
             if m.by != "agent"}
    out = []
    for incident in closed:
        reason = (incident.data or {}).get("dismissed_by_agent")
        if reason:
            how = f"Your coding agent said it isn't your site's problem: {reason}"
        elif (incident.kind, incident.key) in waved:
            how = f"You said it was fine, {ago(incident.ended_at, now)}."
        else:
            how = f"Fixed {ago(incident.ended_at, now)}."
        out.append({"title": incident.title, "how": how})
    return out


async def seven_days(db, project: Project, now: dt.datetime) -> list[dict]:
    """The last seven days in the owner's time zone, each with the worst trouble the site had that day."""
    zone = local_zone(project.timezone) or dt.UTC
    today = now.astimezone(zone).date()
    first = dt.datetime.combine(today - dt.timedelta(days=6), dt.time(), zone)
    rows = (await db.execute(select(Incident.level, Incident.started_at, Incident.ended_at).where(
        Incident.project_id == project.id, Incident.kind.not_in(SITE_TROUBLE_SKIPS), Incident.started_at < now,
        (Incident.ended_at.is_(None)) | (Incident.ended_at >= first)))).all()
    created = project.created_at.astimezone(zone).date()
    days = []
    for n in range(7):
        start = first + dt.timedelta(days=n)
        end = start + dt.timedelta(days=1)
        levels = {r.level for r in rows if r.started_at < end and (r.ended_at is None or r.ended_at >= start)}
        state = "broken" if "broken" in levels else "look" if "look" in levels else \
            "none" if start.date() < created else "working"
        label = {"broken": "broken for a while", "look": "something needed a look", "working": "no trouble",
                 "none": "not watched yet"}[state]
        days.append({"state": state, "label": f"{start:%a %-d %b}: {label}"})
    return days


async def render_page(request: Request, token: str, sent: str | None, name: str, tab: str) -> HTMLResponse:
    context = await page_context(request, token, sent)
    return templates.TemplateResponse(request, name, {**context, "tab": tab, "domain": context["page"].domain},
                                      headers=PRIVATE)


@router.get("/d/{token}", response_class=HTMLResponse)
async def page(request: Request, token: str, sent: str | None = None) -> HTMLResponse:
    """What there is to do, most urgent first."""
    return await render_page(request, token, sent, "page.html", "todo")


@router.get("/d/{token}/week", response_class=HTMLResponse)
async def week(request: Request, token: str, sent: str | None = None) -> HTMLResponse:
    """The site this week: numbers, sales, pages, where visitors came from, jobs and history."""
    return await render_page(request, token, sent, "week.html", "week")


@router.get("/d/{token}/settings", response_class=HTMLResponse)
async def settings_page(request: Request, token: str, sent: str | None = None) -> HTMLResponse:
    """Where alerts go, and the site's key."""
    return await render_page(request, token, sent, "settings.html", "settings")


def setup_steps(project: Project, summary, emailed: bool, items: dict | None = None) -> list[dict]:
    """The setup checklist. Two steps are done before the owner ever sees
    it, so it never starts at zero. The later ones follow the same checklist
    as `npx prodbat setup`: one the agent skipped (no job queue, no payments)
    shows as skipped with its reason, and one it finished waits for its first
    data without asking for anything more."""
    items = items or {}

    def later(sid: str, what: str, done: bool) -> dict:
        step = {"what": what, "done": done, "later": True, "how": f"Run `npx prodbat suggestions {sid}` and follow it."}
        item = items.get(sid)
        if done or item is None:
            return step
        if item.state == "done":
            step["done"] = True
        elif item.state == "skipped":
            step |= {"done": True, "skipped": True, "note": f"Skipped: {item.reason}." if item.reason else "Skipped."}
        elif item.state == "waiting":
            step |= {"waiting": True, "note": "Set up. This ticks itself when the first data arrives."}
        return step

    return [
        {"what": "Your agent created the project", "done": True},
        {"what": "Alerts have an address to go to", "done": bool(project.alert_email),
         "how": "Run `npx prodbat init --email you@example.com` again with your address."},
        {"what": "Your site sends its first visit", "done": project.first_data_at is not None,
         "how": "Happens after your agent deploys the script tag."},
        later("S-03", "Your server reports in", project.first_server_at is not None),
        {"what": "A test alert reaches your inbox", "done": emailed, "later": True, "test": True},
        later("S-01", "Payments: forward your Stripe events", summary.payments_connected),
        later("S-02", "Scheduled jobs", False),
        later("S-06", "Job queue", False),
        later("S-07", "Logs", summary.has_logs),
    ]


def stalled(project: Project) -> bool:
    """Still waiting a quarter of an hour after the project was made."""
    return project.first_data_at is None and \
        dt.datetime.now(dt.UTC) - project.created_at > dt.timedelta(minutes=15)


@router.post("/d/{token}/mute/{incident_id}")
async def mute(request: Request, token: str, incident_id: int) -> RedirectResponse:
    """This is fine, stop telling me."""
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        incident = await db.get(Incident, incident_id)
        if incident is None or incident.project_id != project.id:
            raise HTTPException(404, "no such problem")
        await db.execute(pg_insert(Mute).values(project_id=project.id, kind=incident.kind, key=incident.key)
                         .on_conflict_do_nothing())
        if incident.ended_at is None:
            resolve(db, project, incident, request.app.state.settings, dt.datetime.now(dt.UTC))
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent=muted", status_code=303)


@router.post("/d/{token}/test-alert")
async def test_alert(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("test-alert", str(project.id), 5, 3600)
        await queue_test_alert(db, project, request.app.state.settings)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=test#test-alert", status_code=303)


# ---- text messages ------------------------------------------------------------

@router.post("/d/{token}/sms")
async def sms_start(request: Request, token: str, number: str = Form(...)) -> RedirectResponse:
    """Text a six-digit code to the number; it becomes the alert number once confirmed."""
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not can_text(project, settings):
            raise HTTPException(403, "Text messages are not available for this site.")
        clean = normalize_number(number)
        if clean is None:
            return RedirectResponse(f"/d/{token}/settings?sent=badnumber#texts", status_code=303)
        if settings.text_prefixes and not clean.startswith(tuple(settings.text_prefixes)):
            return RedirectResponse(f"/d/{token}/settings?sent=textcountry#texts", status_code=303)
        request.app.state.limiter.check("sms", str(project.id), 3, 3600)
        start_confirming(db, project, settings, clean)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=smscode#texts", status_code=303)


@router.post("/d/{token}/sms/confirm")
async def sms_confirm(request: Request, token: str, code: str = Form(...)) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("sms-confirm", str(project.id), 10, 3600)
        ok = confirm(project, code)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent={'smson' if ok else 'smswrong'}#texts", status_code=303)


@router.post("/d/{token}/sms/remove")
async def sms_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        project.sms_to = project.sms_pending = project.sms_code_hash = None
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=smsoff#texts", status_code=303)


# ---- Slack --------------------------------------------------------------------

@router.post("/d/{token}/slack")
async def slack_start(request: Request, token: str) -> RedirectResponse:
    """Off to Slack to pick a channel; Slack sends the owner back to /slack/callback."""
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not (settings.slack_configured and project.slack_enabled):
            raise HTTPException(403, "Slack is not available for this site.")
        url = slack.authorize_url(project, settings)
        await db.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/slack/callback")
async def slack_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    settings = request.app.state.settings
    project_hex, _, nonce = state.partition(".")
    try:
        project_id = uuid.UUID(hex=project_hex)
    except ValueError:
        raise HTTPException(400, "This link from Slack is not one we made.")
    async with request.app.state.sessions() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.slack_state or not nonce \
                or not hmac.compare_digest(project.slack_state, nonce):
            raise HTTPException(400, "This link from Slack is not one we made, or it was already used.")
        guard = request.app.state.page_guard
        if guard is not None:
            await guard(request, project)
        token = project.page_token
        project.slack_state = None
        problem = "cancelled" if error or not code else \
            await slack.finish_connecting(request.app.state.http, settings, project, code)
        if problem is None:
            slack.post_connected(db, project, settings)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent={'slackno' if problem else 'slackon'}#slack", status_code=303)


@router.post("/d/{token}/slack/remove")
async def slack_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        await slack.disconnect(request.app.state.http, project)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=slackoff#slack", status_code=303)


# ---- Telegram -------------------------------------------------------------------

@router.post("/d/{token}/telegram")
async def telegram_start(request: Request, token: str) -> RedirectResponse:
    """A new one-time code; the page then shows the links that carry it to the bot."""
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not telegram.can_connect(project, settings):
            raise HTTPException(403, "Telegram is not available for this site.")
        request.app.state.limiter.check("telegram", str(project.id), 10, 3600)
        telegram.start_connecting(project)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings#telegram", status_code=303)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    """Where Telegram sends what people write to the bot. The secret header
    proves it is Telegram; the answer, if any, is a reply Telegram sends for us."""
    settings = request.app.state.settings
    if not settings.telegram_configured:
        raise HTTPException(404, "Not Found")
    given = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(given.encode(), settings.telegram_webhook_secret.encode()):
        raise HTTPException(403, "Wrong secret.")
    try:
        update = await request.json()
    except ValueError:
        return {}
    if not isinstance(update, dict):
        return {}
    async with request.app.state.sessions() as db:
        answer = await telegram.handle_update(db, settings, update)
        await db.commit()
    return answer or {}


@router.post("/d/{token}/telegram/remove")
async def telegram_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        await telegram.disconnect(db, project)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=telegramoff#telegram", status_code=303)


# ---- a signed webhook -------------------------------------------------------------

def show_secret(request: Request, project: Project, token: str, new: bool) -> HTMLResponse:
    return templates.TemplateResponse(request, "webhook_secret.html", {
        "domain": project.domain, "token": token, "secret": project.webhook_secret, "url": project.webhook_url,
        "new": new}, headers=PRIVATE)


@router.post("/d/{token}/webhook", response_class=HTMLResponse)
async def webhook_add(request: Request, token: str, url: str = Form(...)):
    """Keep the address and make its signing secret, then show the secret."""
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not webhooks.can_add(project, settings):
            raise HTTPException(403, "Webhooks are not available for this site.")
        request.app.state.limiter.check("webhook", str(project.id), 10, 3600)
        if await webhooks.check_url_resolves_publicly(url):
            return RedirectResponse(f"/d/{token}/settings?sent=webhookbad#webhook", status_code=303)
        webhooks.connect(project, url)
        await db.commit()
    return show_secret(request, project, token, new=True)


@router.post("/d/{token}/webhook/secret", response_class=HTMLResponse)
async def webhook_secret(request: Request, token: str, rotate: str = Form("")):
    """Show the signing secret again, or replace it (the old one stops at once)."""
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not project.webhook_url:
            raise HTTPException(404, "There is no webhook.")
        if rotate:
            request.app.state.limiter.check("webhook", str(project.id), 10, 3600)
            project.webhook_secret = webhooks.new_secret()
            await db.commit()
    return show_secret(request, project, token, new=bool(rotate))


@router.post("/d/{token}/webhook/test")
async def webhook_test(request: Request, token: str) -> RedirectResponse:
    """Send a test event. After the webhook was turned off, this turns it on again."""
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("test-alert", str(project.id), 5, 3600)
        if project.webhook_error:
            webhooks.turn_on_again(project)
        queued = webhooks.post_test(db, project, request.app.state.settings)
        await db.commit()
    if not queued:
        raise HTTPException(400, "There is no webhook to test.")
    return RedirectResponse(f"/d/{token}/settings?sent=webhooktest#webhook", status_code=303)


@router.post("/d/{token}/webhook/remove")
async def webhook_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        webhooks.disconnect(project)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=webhookoff#webhook", status_code=303)


# ---- GitHub issues ----------------------------------------------------------------

# The one-time state, and the nonce for picking a repository afterwards. Both are
# random values bound to one project; neither is a GitHub token.
GITHUB_COOKIE = "pb_github"
GITHUB_PICK_COOKIE = "pb_github_pick"
NOT_OURS = "This link from GitHub is not one we made, it was already used, or it was opened in another browser."


def _set_cookie(response, settings, name: str, value: str, life: dt.timedelta) -> None:
    response.set_cookie(name, value, max_age=int(life.total_seconds()), httponly=True, samesite="lax",
                        secure=settings.base.startswith("https://"))


def _off_to_github(settings, url: str, state: str) -> RedirectResponse:
    """Send the owner to GitHub with a new state. GitHub hands it back in the address,
    and the cookie holds it too, so it must come back to the browser that left."""
    response = RedirectResponse(url, status_code=303)
    _set_cookie(response, settings, GITHUB_COOKIE, state, github.STATE_LIFE)
    return response


async def _start(request: Request, token: str, where) -> RedirectResponse:
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not github.can_connect(project, settings):
            raise HTTPException(403, "GitHub is not available for this site.")
        request.app.state.limiter.check("github", str(project.id), 10, 3600)
        state = github.new_state(project)
        await db.commit()
    return _off_to_github(settings, where(settings, state), state)


@router.post("/d/{token}/github")
async def github_start(request: Request, token: str) -> RedirectResponse:
    """Off to GitHub's "Authorize Prodbat" screen; GitHub sends the owner back to /github/callback."""
    return await _start(request, token, github.authorize_url)


@router.post("/d/{token}/github/install")
async def github_install(request: Request, token: str) -> RedirectResponse:
    """Off to GitHub to install the app, or give it another repository. GitHub comes back to /github/callback."""
    return await _start(request, token, github.install_url)


@router.get("/github/setup")
async def github_setup(request: Request) -> RedirectResponse:
    """An App set up before user authorization still sends owners here; carry on at the callback."""
    query = request.url.query
    return RedirectResponse("/github/callback" + (f"?{query}" if query else ""), status_code=303)


@router.get("/github/callback")
async def github_callback(request: Request, code: str = "", state: str = "", installation_id: str = "",
                          error: str = "") -> RedirectResponse:
    """GitHub sends the owner back here after "Authorize Prodbat", and after installing
    the app. The installation they connect must be one GitHub lists for their own
    account; the id in the address is only a hint."""
    settings = request.app.state.settings
    if not settings.github_configured:
        raise HTTPException(404, "Not Found")
    # The state must come back to the browser that left (the cookie); if GitHub
    # also hands it back in the address, the two must agree.
    cookie = request.cookies.get(GITHUB_COOKIE, "")
    if not cookie or (state and not github.same(state, cookie)):
        raise HTTPException(400, NOT_OURS)
    project_hex, _, nonce = cookie.partition(".")
    try:
        project_id = uuid.UUID(hex=project_hex)
    except ValueError:
        raise HTTPException(400, NOT_OURS)
    http = request.app.state.http
    async with request.app.state.sessions() as db:
        project = await db.get(Project, project_id)
        if not github.state_is_good(project, nonce):
            raise HTTPException(400, NOT_OURS)
        guard = request.app.state.page_guard
        if guard is not None:
            await guard(request, project)
        token = project.page_token
        project.github_state = project.github_state_at = None  # it works once
        problem, sent, pick_nonce = None, "githubon", None
        if not github.can_connect(project, settings):
            problem = "GitHub is not available for this site."
        elif error:
            sent = "githubno"  # they pressed Cancel on GitHub
        elif not code:
            # Back from installing without authorizing (an App without user
            # authorization during installation): ask GitHub who this is now.
            state = github.new_state(project)
            await db.commit()
            return _off_to_github(settings, github.authorize_url(settings, state), state)
        else:
            try:
                user_token = await github.user_token(http, settings, code)
                reach = await github.what_the_user_can_reach(http, settings, user_token)
            except github.Refused as exc:
                problem = str(exc)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                problem = github.NO_ANSWER
            finally:
                user_token = None  # used for this request only, never kept
            if problem is None:
                found = reach["installations"]
                if installation_id:
                    found = [i for i in found if str(i["id"]) == installation_id]
                    if not found:
                        problem = github.NOT_YOURS
                elif not found:
                    # Nothing installed that this GitHub account can use: install it first.
                    state = github.new_state(project)
                    await db.commit()
                    return _off_to_github(settings, github.install_url(settings, state), state)
            if problem is None:
                reach["installations"] = found
                every = [(i, r["name"]) for i in found for r in i["repos"]]
                if len(every) == 1:
                    await github.connect(db, project, every[0][0], every[0][1], reach.get("login"))
                else:
                    pick_nonce = github.hold_choices(project, reach)
                    sent = "githubpick"
        if problem:
            project.github_note, sent = problem, "githubno"
        await db.commit()
    response = RedirectResponse(f"/d/{token}/settings?sent={sent}#github", status_code=303)
    response.delete_cookie(GITHUB_COOKIE)
    if pick_nonce:
        _set_cookie(response, settings, GITHUB_PICK_COOKIE, f"{project_id.hex}.{pick_nonce}", github.PICK_LIFE)
    return response


@router.post("/d/{token}/github/pick")
async def github_pick(request: Request, token: str, choice: str = Form(...)) -> RedirectResponse:
    """Connect the repository picked from what GitHub listed for the owner, from the browser that went to GitHub."""
    project_hex, _, nonce = request.cookies.get(GITHUB_PICK_COOKIE, "").partition(".")
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not github.can_connect(project, request.app.state.settings) or project_hex != project.id.hex \
                or not await github.pick(db, project, nonce, choice):
            raise HTTPException(400, "That repository wasn't in the list GitHub gave, or the list ran out "
                                     "(it lasts 10 minutes). Connect GitHub again.")
        await db.commit()
    response = RedirectResponse(f"/d/{token}/settings?sent=githubon#github", status_code=303)
    response.delete_cookie(GITHUB_PICK_COOKIE)
    return response


@router.post("/d/{token}/github/settings")
async def github_settings(request: Request, token: str, look: str = Form(""), mention: str = Form(""),
                          assignees: str = Form("")) -> RedirectResponse:
    names = github.clean_assignees(assignees)
    if names is None:
        raise HTTPException(400, "Assignees are GitHub logins separated by commas, such as ann, octocat (up to 10).")
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        project.github_look = bool(look)
        project.github_mention = github.clean_mention(mention)
        project.github_assignees = ", ".join(names) or None
        project.github_note = None
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=githubsaved#github", status_code=303)


@router.post("/d/{token}/github/test")
async def github_test(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("test-alert", str(project.id), 5, 3600)
        queued = github.post_test(db, project, request.app.state.settings)
        await db.commit()
    if not queued:
        raise HTTPException(400, "Pick a repository first.")
    return RedirectResponse(f"/d/{token}/settings?sent=githubtest#github", status_code=303)


@router.post("/d/{token}/github/remove")
async def github_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        await github.disconnect(db, project)
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=githuboff#github", status_code=303)


# ---- notifications on a device ---------------------------------------------------

@router.get("/d/{token}/manifest.webmanifest")
async def manifest(request: Request, token: str) -> JSONResponse:
    """So the page can be added to a phone's Home Screen; on an iPhone that is
    what lets it show notifications. Browsers fetch this without cookies, so it
    is not behind sign-in; it says no more than the domain."""
    async with request.app.state.sessions() as db:
        project = await db.scalar(select(Project).where(Project.page_token == token))
    if project is None:
        raise HTTPException(404, "no such page")
    base = request.app.state.settings.base
    icons = [{"src": f"{base}/static/icon-{size}.png", "sizes": f"{size}x{size}", "type": "image/png",
              "purpose": "any maskable"} for size in (192, 512)]
    return JSONResponse({"name": f"Prodbat · {project.domain}", "short_name": project.domain[:12],
                         "start_url": f"/d/{token}", "scope": f"/d/{token}", "display": "standalone",
                         "background_color": "#F4F5F1", "theme_color": "#F4F5F1", "icons": icons},
                        media_type="application/manifest+json", headers=PRIVATE)


@router.post("/d/{token}/push")
async def push_subscribe(request: Request, token: str) -> dict:
    """The browser's subscription, sent by the page's script after the owner allowed notifications."""
    settings = request.app.state.settings
    try:
        sent = await request.json()
        endpoint, keys = str(sent["endpoint"]), sent["keys"]
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "The browser didn't send a subscription.")
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        if not push.can_push(project, settings):
            raise HTTPException(403, "Notifications are not available for this site.")
        request.app.state.limiter.check("push", str(project.id), 20, 3600)
        if problem := push.check_subscription(endpoint, p256dh, auth):
            raise HTTPException(400, problem)
        known = await db.scalar(select(PushSubscription.id).where(PushSubscription.endpoint == endpoint))
        if known is None and len(await push.devices(db, project, settings)) >= push.MAX_DEVICES:
            raise HTTPException(400, f"A site can notify up to {push.MAX_DEVICES} devices. Remove one first.")
        device = await push.subscribe(db, project, endpoint, p256dh, auth, request.headers.get("user-agent", ""))
        if known is None:
            push.post_hello(db, project, settings, device)
        await db.commit()
    return {"ok": True, "label": device.label}


@router.post("/d/{token}/push/{device_id}/remove")
async def push_remove(request: Request, token: str, device_id: int) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        device = await db.get(PushSubscription, device_id)
        if device is None or device.project_id != project.id:
            raise HTTPException(404, "no such device")
        await db.delete(device)
        project.push_error = None
        await db.commit()
    return RedirectResponse(f"/d/{token}/settings?sent=pushoff#push", status_code=303)


# ---- a new secret key ---------------------------------------------------------

@router.post("/d/{token}/key", response_class=HTMLResponse)
async def new_key(request: Request, token: str) -> HTMLResponse:
    """Replace the project's secret key and show the new one once. The old
    key stops working at once, so the app's server and the CLI need the new one."""
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("new-key", str(project.id), 5, 3600)
        secret = new_secret()
        project.secret_hash = hash_secret(secret)
        await db.commit()
    return templates.TemplateResponse(request, "key.html", {
        "domain": project.domain, "token": token, "secret": secret, "server": settings.base}, headers=PRIVATE)


@router.post("/d/{token}/timezone")
async def set_timezone(request: Request, token: str, tz: str = Form(...)) -> dict:
    """The page sends the browser's time zone once, for email times and the Monday email."""
    if local_zone(tz) is None or len(tz) > 64:
        raise HTTPException(400, "Unknown time zone.")
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        project.timezone = tz
        await db.commit()
    return {"ok": True}
