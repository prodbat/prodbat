"""Sends what is in the outbox, over plain SMTP so any provider works."""

import asyncio
import datetime as dt
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from sqlalchemy import select

from .config import Settings
from .models import Email

MAX_ATTEMPTS = 5


def build(email: Email, settings: Settings) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((email.sender_name, parseaddr(settings.mail_from)[1]))
    message["To"] = email.to
    message["Subject"] = email.subject
    message["Message-ID"] = email.message_id
    if email.in_reply_to:
        message["In-Reply-To"] = email.in_reply_to
        message["References"] = email.in_reply_to
    message.set_content(email.body)
    return message


def deliver(message: EmailMessage, settings: Settings) -> None:
    if not settings.smtp_host:
        print(f"--- email not sent (PRODBAT_SMTP_HOST is not set) ---\n{message}\n---", flush=True)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(message)


async def send_pending(sessions, settings: Settings, deliver=deliver) -> int:
    sent = 0
    async with sessions() as db:
        emails = (await db.scalars(
            select(Email).where(Email.sent_at.is_(None), Email.attempts < MAX_ATTEMPTS)
            .order_by(Email.created_at).limit(50).with_for_update(skip_locked=True))).all()
        for email in emails:
            email.attempts += 1
            try:
                await asyncio.to_thread(deliver, build(email, settings), settings)
            except Exception as exc:  # a mail server can fail in many ways; keep the rest moving
                email.last_error = f"{type(exc).__name__}: {exc}"[:500]
            else:
                email.sent_at = dt.datetime.now(dt.UTC)
                email.last_error = None
                sent += 1
        await db.commit()
    return sent
