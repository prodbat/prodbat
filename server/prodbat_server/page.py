"""The one page: a status sentence, five numbers and four lists.

There is no sign-in in the open-source server; the link is the secret.
"""

import datetime as dt
import hmac
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import slack
from .api import queue_test_alert, script_tag, test_destinations
from .incidents import agent_prompt, resolve
from .models import Email, Incident, Mute, Project, SiteCheck, hash_secret, new_secret
from .summary import gather
from .texts import can_text, confirm, normalize_number, start_confirming
from .words import local_zone

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

@router.get("/d/{token}", response_class=HTMLResponse)
async def page(request: Request, token: str, sent: str | None = None) -> HTMLResponse:
    settings = request.app.state.settings
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        summary = await gather(db, project, dt.datetime.now(dt.UTC))
        broken = next((p for p in summary.problems if p.level == "broken" and p.id), None)
        prompt = None
        if broken:
            prompt = agent_prompt(project, await db.get(Incident, broken.id))
        emailed = await db.scalar(select(Email.id).where(Email.project_id == project.id).limit(1)) is not None
        checks = await db.scalar(select(func.count()).select_from(SiteCheck)
                                 .where(SiteCheck.project_id == project.id)) or 0
    notice = {"test": f"A test alert is on its way to {test_destinations(project, settings)}.",
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
              "slackoff": "Slack is disconnected. Alerts still go by email."}.get(sent or "")
    setup = setup_steps(project, summary, emailed)
    return templates.TemplateResponse(request, "page.html", {
        "page": summary, "token": token, "script_tag": script_tag(settings, project), "broken": broken,
        "prompt": prompt, "alert_email": project.alert_email, "notice": notice, "setup": setup,
        "unfinished": [s for s in setup if not s["done"] and s.get("later")],
        "paused": project.paused_at is not None, "checks": checks,
        "since": f"{project.created_at:%-d %b}", "stalled": stalled(project),
        "texts": can_text(project, settings), "text_where": text_where(settings) if settings.text_prefixes else None, "sms_to": project.sms_to, "sms_pending": project.sms_pending,
        "needs_timezone": not project.timezone,
        "slack": settings.slack_configured and project.slack_enabled, "slack_channel": project.slack_channel,
        "slack_connected": bool(project.slack_token), "slack_team": project.slack_team,
        "slack_error": project.slack_error,
        "can_test": bool(project.alert_email) or slack.can_post(project, settings),
    }, headers=PRIVATE)


def setup_steps(project: Project, summary, emailed: bool) -> list[dict]:
    """The setup checklist. Two steps are done before the owner ever sees
    it, so it never starts at zero."""
    return [
        {"what": "Your agent created the project", "done": True},
        {"what": "Alerts have an address to go to", "done": bool(project.alert_email),
         "how": "Run `npx prodbat init --email you@example.com` again with your address."},
        {"what": "Your site sends its first visit", "done": project.first_data_at is not None,
         "how": "Happens after your agent deploys the script tag."},
        {"what": "Your server reports in", "done": project.first_server_at is not None, "later": True,
         "how": "Run `npx prodbat suggestions S-03` and follow it."},
        {"what": "A test alert reaches your inbox", "done": emailed, "later": True, "test": True},
        {"what": "Payments: forward your Stripe events", "done": summary.payments_connected, "later": True,
         "how": "Run `npx prodbat suggestions S-01` and follow it."},
        {"what": "Scheduled jobs and queues", "done": bool(summary.work), "later": True,
         "how": "Run `npx prodbat suggestions S-06` and follow it."},
        {"what": "Logs", "done": summary.has_logs, "later": True,
         "how": "Run `npx prodbat suggestions S-07` and follow it."},
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
        queue_test_alert(db, project, request.app.state.settings)
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent=test", status_code=303)


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
            return RedirectResponse(f"/d/{token}?sent=badnumber#texts", status_code=303)
        if settings.text_prefixes and not clean.startswith(tuple(settings.text_prefixes)):
            return RedirectResponse(f"/d/{token}?sent=textcountry#texts", status_code=303)
        request.app.state.limiter.check("sms", str(project.id), 3, 3600)
        start_confirming(db, project, settings, clean)
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent=smscode#texts", status_code=303)


@router.post("/d/{token}/sms/confirm")
async def sms_confirm(request: Request, token: str, code: str = Form(...)) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        request.app.state.limiter.check("sms-confirm", str(project.id), 10, 3600)
        ok = confirm(project, code)
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent={'smson' if ok else 'smswrong'}#texts", status_code=303)


@router.post("/d/{token}/sms/remove")
async def sms_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        project.sms_to = project.sms_pending = project.sms_code_hash = None
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent=smsoff#texts", status_code=303)


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
    return RedirectResponse(f"/d/{token}?sent={'slackno' if problem else 'slackon'}#slack", status_code=303)


@router.post("/d/{token}/slack/remove")
async def slack_remove(request: Request, token: str) -> RedirectResponse:
    async with request.app.state.sessions() as db:
        project = await project_by_token(request, db, token)
        await slack.disconnect(request.app.state.http, project)
        await db.commit()
    return RedirectResponse(f"/d/{token}?sent=slackoff#slack", status_code=303)


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
