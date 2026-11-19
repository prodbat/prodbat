"""Alerts in a Telegram chat or group, alongside email.

The owner presses "Connect Telegram" on their page and gets a link to the bot
with a one-time code in it. Pressing Start in Telegram sends the bot
"/start <code>"; Telegram passes that to /telegram/webhook, and the chat it came
from becomes where alerts go. The same works in a group: add the bot with the
second link, or send "/start <code>" there. Each problem is one message, and
everything later about it replies to that message.

Text that strangers wrote (error messages, routes) is escaped for Telegram's
HTML, and an @ in it can't mention anyone in a group.
"""

import datetime as dt
import html
import re
import secrets
import time

import httpx
from sqlalchemy import select, update

from .config import Settings
from .models import Incident, Project, TelegramMessage

API = "https://api.telegram.org"
CODE_LIFE = dt.timedelta(hours=1)
MAX_ATTEMPTS = 5
BLOCKED = "Prodbat's bot was blocked, or removed from the chat. Connect Telegram again to get alerts there."
NO_CHAT = "The Telegram chat is gone. Connect Telegram again to get alerts there."
# After Telegram says to slow down, nothing is sent until this time.
_quiet_until = 0.0


def can_connect(project: Project, settings: Settings) -> bool:
    return bool(settings.telegram_configured and project.telegram_enabled)


def can_post(project: Project, settings: Settings) -> bool:
    return bool(can_connect(project, settings) and project.paused_at is None and project.telegram_chat_id
                and not project.telegram_error)


def esc(value: str) -> str:
    """For Telegram's HTML: & < > as entities, and a word joiner after each @ so
    a stranger's "@someone" doesn't mention (and notify) anyone in a group."""
    return html.escape(value or "", quote=False).replace("@", "@⁠")


def cut(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


# ---- connecting -------------------------------------------------------------------

def start_connecting(project: Project, now: dt.datetime | None = None) -> None:
    project.telegram_code = secrets.token_urlsafe(16)  # letters, digits, - and _: what Telegram allows
    project.telegram_code_at = now or dt.datetime.now(dt.UTC)


def pending_code(project: Project, now: dt.datetime | None = None) -> str | None:
    now = now or dt.datetime.now(dt.UTC)
    if project.telegram_code and project.telegram_code_at and now - project.telegram_code_at < CODE_LIFE:
        return project.telegram_code
    return None


def links(project: Project, settings: Settings) -> dict | None:
    code = pending_code(project)
    if code is None:
        return None
    bot = f"https://t.me/{settings.telegram_bot_name.lstrip('@')}"
    return {"chat": f"{bot}?start={code}", "group": f"{bot}?startgroup={code}"}


def chat_name(chat: dict) -> str:
    if chat.get("type") in ("group", "supergroup", "channel"):
        return cut(chat.get("title") or "your group", 100)
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
    return cut(name or (f"@{chat['username']}" if chat.get("username") else "your chat"), 100)


async def forget_threads(db, project: Project) -> None:
    """Message ids belong to one chat; in another they would point at the wrong message."""
    await db.execute(update(Incident).where(Incident.project_id == project.id)
                     .values(telegram_message_id=None))


START = re.compile(r"/start(?:@(\w+))?(?:\s+(\S+))?\s*")


async def handle_update(db, settings: Settings, update_: dict, now: dt.datetime | None = None) -> dict | None:
    """One update from Telegram. Only "/start <code>" matters; the answer is a
    sendMessage that Telegram carries out for us, or None to say nothing."""
    message = update_.get("message")
    if not isinstance(message, dict):
        return None
    found = START.fullmatch(message.get("text") or "")
    chat = message.get("chat") or {}
    if not found or not isinstance(chat.get("id"), int):
        return None
    if found.group(1) and found.group(1).lower() != settings.telegram_bot_name.lstrip("@").lower():
        return None  # a /start meant for another bot in the same group

    def reply(text: str) -> dict:
        return {"method": "sendMessage", "chat_id": chat["id"], "text": text}

    code = found.group(2)
    if not code:
        return reply("To get Prodbat alerts here, open your Prodbat page, press Connect Telegram and use the "
                     "link it gives you.")
    project = await db.scalar(select(Project).where(Project.telegram_code == code))
    if project is None or pending_code(project, now) != code or not can_connect(project, settings):
        return reply("That link has run out or was already used. Press Connect Telegram on your Prodbat page "
                     "for a new one.")
    await forget_threads(db, project)
    project.telegram_chat_id, project.telegram_chat = chat["id"], chat_name(chat)
    project.telegram_code = project.telegram_code_at = project.telegram_error = None
    return reply(f"Connected. Prodbat alerts for {project.domain} will arrive here.")


async def disconnect(db, project: Project) -> None:
    await forget_threads(db, project)
    project.telegram_chat_id = project.telegram_chat = project.telegram_error = None
    project.telegram_code = project.telegram_code_at = None


async def register_webhook(http: httpx.AsyncClient, settings: Settings) -> str:
    """Tell Telegram where to send the bot's updates. Safe to repeat."""
    response = await http.post(f"{API}/bot{settings.telegram_bot_token}/setWebhook", json={
        "url": f"{settings.base}/telegram/webhook", "secret_token": settings.telegram_webhook_secret,
        "allowed_updates": ["message"]})
    answer = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if not answer.get("ok"):
        raise RuntimeError(f"Telegram answered {response.status_code}: {answer.get('description', response.text[:200])}")
    return answer.get("description") or "Webhook was set"


# ---- what is sent -------------------------------------------------------------------

def _post(db, project: Project, settings: Settings, text: str, incident: Incident | None = None) -> bool:
    if not can_post(project, settings):
        return False
    link = f'<a href="{html.escape(settings.base)}/d/{project.page_token}">Open your Prodbat page</a>'
    db.add(TelegramMessage(project_id=project.id, incident=incident, text=f"{text}\n\n{link}"))
    return True


def post_alert(db, project: Project, incident: Incident, settings: Settings, prefix: str, todo: str,
               prompt: str, when: str) -> None:
    _post(db, project, settings, "\n".join([
        f"<b>{esc(cut(f'{prefix}: {incident.title} on {project.domain}', 300))}</b>",
        "",
        f"<b>What broke:</b> {esc(cut(incident.what, 1500))}",
        f"<b>Since when:</b> {esc(when)}",
        f"<b>Who is affected:</b> {esc(cut(incident.who, 300))}",
        f"<b>What to do:</b> {esc(cut(todo, 600))}",
        "",
        "For your coding agent:",
        f"<pre>{esc(cut(prompt, 800))}</pre>",
    ]), incident)


def post_recovery(db, project: Project, incident: Incident, settings: Settings, recovery: str, duration: str) -> None:
    _post(db, project, settings, "\n".join([
        f"<b>{esc(cut(f'Working again: {incident.title} on {project.domain}', 300))}</b>",
        esc(cut(recovery, 600)), f"It was broken for {esc(duration)}."]), incident)


def post_digest(db, project: Project, settings: Settings, new: list[Incident], about: str) -> None:
    lines = [f"<b>{esc(f'Needs a look: {len(new)} things about {about} on {project.domain}')}</b>",
             "None of them is breaking your site right now.", ""]
    lines += [f"• <b>{esc(cut(i.title, 200))}</b>. {esc(cut(i.todo, 200))}" for i in new[:10]]
    if len(new) > 10:
        lines.append(f"And {len(new) - 10} more on your page.")
    _post(db, project, settings, "\n".join(lines))


def post_test(db, project: Project, settings: Settings) -> bool:
    return _post(db, project, settings, "\n".join([
        f"<b>{esc(f'Test: this is what a Prodbat alert for {project.domain} looks like')}</b>",
        "This is a test. Nothing is wrong.", "",
        "<b>What broke:</b> POST /api/checkout answered with an error (500) for 4 visitors in the last 15 minutes.",
        "<b>Since when:</b> 14:10 UTC today.", "<b>Who is affected:</b> 4 visitors so far.",
        "<b>What to do:</b> Paste the line in the alert into your coding agent, or roll back your last deploy."]))


# ---- sending ----------------------------------------------------------------------

async def _send(http: httpx.AsyncClient, settings: Settings, project: Project,
                message: TelegramMessage) -> tuple[str | None, dict]:
    """Send one message. Returns what went wrong (or None) and Telegram's parameters."""
    payload = {"chat_id": project.telegram_chat_id, "text": message.text, "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": True}}
    first = message.incident.telegram_message_id if message.incident is not None else None
    if first:
        payload["reply_parameters"] = {"message_id": first, "allow_sending_without_reply": True}
    response = await http.post(f"{API}/bot{settings.telegram_bot_token}/sendMessage", json=payload)
    try:
        answer = response.json()
    except ValueError:
        answer = {}
    if answer.get("ok"):
        if message.incident is not None and not message.incident.telegram_message_id:
            message.incident.telegram_message_id = (answer.get("result") or {}).get("message_id")
        return None, {}
    code = answer.get("error_code", response.status_code)
    return f"{code} {answer.get('description', '')}".strip(), answer.get("parameters") or {}


async def send_pending_telegram(sessions, settings: Settings, http: httpx.AsyncClient) -> int:
    global _quiet_until
    if not settings.telegram_configured or time.monotonic() < _quiet_until:
        return 0
    sent = 0
    async with sessions() as db:
        messages = (await db.scalars(
            select(TelegramMessage).where(TelegramMessage.sent_at.is_(None), TelegramMessage.attempts < MAX_ATTEMPTS)
            .order_by(TelegramMessage.created_at, TelegramMessage.id).limit(50)
            .with_for_update(skip_locked=True))).all()
        for message in messages:
            project = await db.get(Project, message.project_id)
            await db.refresh(message, ["incident"])
            if not project.telegram_chat_id or project.telegram_error:
                message.attempts, message.last_error = MAX_ATTEMPTS, "Telegram is not connected"
                continue
            message.attempts += 1
            try:
                error, params = await _send(http, settings, project, message)
                if params.get("migrate_to_chat_id"):  # a group that became a supergroup has a new id
                    project.telegram_chat_id = params["migrate_to_chat_id"]
                    error, params = await _send(http, settings, project, message)
            except Exception as exc:  # keep the rest moving
                error, params = f"{type(exc).__name__}: {exc}", {}
            if error is None:
                message.sent_at, message.last_error = dt.datetime.now(dt.UTC), None
                sent += 1
            elif error.startswith("429"):
                message.attempts -= 1
                _quiet_until = time.monotonic() + int(params.get("retry_after") or 30)
                break
            else:
                message.last_error = error[:500]
                gone = BLOCKED if error.startswith("403") else \
                    NO_CHAT if error.startswith("400") and "chat not found" in error.lower() else None
                if gone:
                    project.telegram_error, project.telegram_chat_id = gone, None
                    message.attempts = MAX_ATTEMPTS
        await db.commit()
    return sent
