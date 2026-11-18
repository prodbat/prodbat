"""Text messages when customers are affected: confirming a number, queueing
a text with a "Broken:" alert, and sending through Twilio's API."""

import datetime as dt
import hashlib
import hmac
import re
import secrets

import httpx
from sqlalchemy import select

from .config import Settings
from .models import Project, TextMessage

CODE_LIFE = dt.timedelta(minutes=10)
MAX_ATTEMPTS = 5
E164 = re.compile(r"\+[1-9]\d{7,14}")


def normalize_number(raw: str) -> str | None:
    """+15551234567 style, or None. A number without a country code is refused:
    guessing the country would text the wrong person."""
    digits = re.sub(r"[\s().-]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    return digits if E164.fullmatch(digits) else None


def can_text(project: Project, settings: Settings) -> bool:
    return settings.texts_configured and project.sms_enabled and project.paused_at is None


def queue_text(db, project: Project, settings: Settings, body: str) -> bool:
    if not project.sms_to or not can_text(project, settings):
        return False
    db.add(TextMessage(project_id=project.id, to=project.sms_to, body=body[:300]))
    return True


def _hash(project: Project, code: str) -> str:
    return hashlib.sha256(f"{project.id}:{project.sms_pending}:{code}".encode()).hexdigest()


def start_confirming(db, project: Project, settings: Settings, number: str, now: dt.datetime | None = None) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    project.sms_pending = number
    project.sms_code_hash = _hash(project, code)
    project.sms_code_at = now or dt.datetime.now(dt.UTC)
    db.add(TextMessage(project_id=project.id, to=number,
                       body=f"Prodbat: your code is {code}. It confirms texts for {project.domain}."))


def confirm(project: Project, code: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.UTC)
    if not project.sms_pending or not project.sms_code_hash or not project.sms_code_at:
        return False
    if now - project.sms_code_at > CODE_LIFE or not hmac.compare_digest(project.sms_code_hash,
                                                                       _hash(project, code.strip())):
        return False
    project.sms_to, project.sms_pending, project.sms_code_hash, project.sms_code_at = project.sms_pending, None, None, None
    return True


async def twilio_send(http: httpx.AsyncClient, settings: Settings, to: str, body: str) -> None:
    sender = {"MessagingServiceSid": settings.twilio_from} if settings.twilio_from.startswith("MG") \
        else {"From": settings.twilio_from}
    response = await http.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json",
        data={"To": to, "Body": body, **sender}, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
    if response.status_code >= 300:
        raise RuntimeError(f"Twilio answered {response.status_code}: {response.text[:200]}")


async def send_pending_texts(sessions, settings: Settings, http: httpx.AsyncClient) -> int:
    if not settings.texts_configured:
        return 0
    sent = 0
    async with sessions() as db:
        for message in (await db.scalars(
                select(TextMessage).where(TextMessage.sent_at.is_(None), TextMessage.attempts < MAX_ATTEMPTS)
                .order_by(TextMessage.created_at).limit(50).with_for_update(skip_locked=True))).all():
            message.attempts += 1
            try:
                await twilio_send(http, settings, message.to, message.body)
            except Exception as exc:  # keep the rest moving
                message.last_error = f"{type(exc).__name__}: {exc}"[:500]
            else:
                message.sent_at = dt.datetime.now(dt.UTC)
                message.last_error = None
                sent += 1
        await db.commit()
    return sent
