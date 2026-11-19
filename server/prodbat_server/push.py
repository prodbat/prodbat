"""Alerts as notifications on the owner's phone or computer (web push).

The owner presses "Notify me on this device" on their page; the browser asks
its push service (Google, Mozilla, Apple, Microsoft) for an address and gives
it to us with two keys. Each alert is encrypted for that device (RFC 8291) and
posted to the address with a signed token that says it came from this server
(VAPID, RFC 8292). Nothing else is needed: no app, no account with anyone.

A problem's notifications share a tag, so "Working again" replaces "Broken"
on the device instead of piling up next to it. Notifications show plain text
only, so what strangers wrote can't add markup; it is only shortened.
"""

import base64
import datetime as dt
import json
import os
import re
import struct
import time
import urllib.parse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select

from .config import Settings
from .models import Incident, Project, PushMessage, PushSubscription

MAX_ATTEMPTS = 5
MAX_DEVICES = 20
# Only the browsers' own push services. The address comes from a browser, so
# without this anyone with the page link could make the worker post anywhere.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")


def can_push(project: Project, settings: Settings) -> bool:
    return bool(settings.push_configured and project.push_enabled and project.paused_at is None)


async def devices(db, project: Project, settings: Settings) -> list[PushSubscription]:
    if not can_push(project, settings):
        return []
    return list((await db.scalars(select(PushSubscription).where(PushSubscription.project_id == project.id)
                                  .order_by(PushSubscription.created_at))).all())


# ---- keys -----------------------------------------------------------------------

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_vapid_keys() -> tuple[str, str]:
    """A fresh pair, as the public key browsers want and the private key for the server."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64(public), b64(key.private_numbers().private_value.to_bytes(32, "big"))


def private_key(settings: Settings) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64(settings.vapid_private_key.strip()), "big"), ec.SECP256R1())


def vapid_header(settings: Settings, key: ec.EllipticCurvePrivateKey, endpoint: str) -> str:
    """The Authorization header: a token signed with our key, for this push service only, for 12 hours."""
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
              "sub": settings.vapid_subject}
    signing = b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode()) + "." + \
        b64(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={settings.vapid_public_key.strip()}"


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291: the body of a push message, readable only by the device that holds the keys."""
    device_public = unb64(p256dh)
    device = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device_public)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    sender_public = sender.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    shared = sender.exchange(ec.ECDH(), device)
    ikm = HKDF(hashes.SHA256(), 32, unb64(auth), b"WebPush: info\x00" + device_public + sender_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02: the last (and only) record
    return salt + struct.pack(">IB", 4096, len(sender_public)) + sender_public + record


# ---- devices ----------------------------------------------------------------------

def check_subscription(endpoint: str, p256dh: str, auth: str) -> str | None:
    """None when the browser's subscription looks right, or what is wrong with it."""
    parts = urllib.parse.urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or len(endpoint) > 1000 or not any(host == h or host.endswith("." + h)
                                                                  for h in PUSH_HOSTS):
        return "This browser's push service isn't one we know."
    try:
        key, secret = unb64(p256dh), unb64(auth)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
    except (ValueError, TypeError):
        return "The browser sent keys we can't use."
    if len(key) != 65 or len(secret) != 16:
        return "The browser sent keys we can't use."
    return None


def label(user_agent: str) -> str:
    """"Chrome on Android", "Safari on iPhone": enough to tell devices apart on the page."""
    ua = user_agent or ""
    system = next((name for marker, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                             ("CrOS", "ChromeOS"), ("Windows", "Windows"), ("Macintosh", "Mac"),
                                             ("Linux", "Linux")) if marker in ua), None)
    browser = next((name for marker, name in (("Edg", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
                                              ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
                                              ("Chrome", "Chrome"), ("Safari", "Safari")) if marker in ua), "A browser")
    return f"{browser} on {system}" if system else browser


async def subscribe(db, project: Project, endpoint: str, p256dh: str, auth: str, user_agent: str) -> PushSubscription:
    """Add a device, or move it to this project if it was somewhere else (one browser, one address)."""
    device = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if device is None:
        device = PushSubscription(endpoint=endpoint)
        db.add(device)
    device.project_id, device.p256dh, device.auth, device.label = project.id, p256dh, auth, label(user_agent)
    project.push_error = None
    await db.flush()
    return device


# ---- what is sent -------------------------------------------------------------------

def plain(value: str, limit: int) -> str:
    """One line of plain text, cut at a word near the limit."""
    value = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value or "")).strip()
    if len(value) <= limit:
        return value
    cut = value[:limit - 1].rsplit(" ", 1)[0] if " " in value[limit // 2:limit] else value[:limit - 1]
    return cut.rstrip(" ,.;:") + "…"


def _queue(db, project: Project, settings: Settings, title: str, body: str, incident: Incident | None = None,
           urgent: bool = False, device: PushSubscription | None = None) -> bool:
    if not can_push(project, settings):
        return False
    db.add(PushMessage(project_id=project.id, incident=incident, title=plain(title, 120), body=plain(body, 240),
                       urgent=urgent, subscription_id=device.id if device else None))
    return True


def post_alert(db, project: Project, incident: Incident, settings: Settings, prefix: str) -> None:
    _queue(db, project, settings, f"{prefix}: {incident.title} on {project.domain}", incident.what, incident,
           urgent=incident.level == "broken")


def post_recovery(db, project: Project, incident: Incident, settings: Settings, recovery: str, duration: str) -> None:
    _queue(db, project, settings, f"Working again: {incident.title} on {project.domain}",
           f"{recovery} It was broken for {duration}.", incident, urgent=True)


def post_digest(db, project: Project, settings: Settings, new: list[Incident], about: str) -> None:
    _queue(db, project, settings, f"Needs a look: {len(new)} things about {about} on {project.domain}",
           "None of them is breaking your site right now.")


def post_test(db, project: Project, settings: Settings) -> bool:
    return _queue(db, project, settings, f"Test: a Prodbat alert for {project.domain}",
                  "This is a test. Nothing is wrong. A real alert says what broke, in one sentence.", urgent=True)


def post_hello(db, project: Project, settings: Settings, device: PushSubscription) -> None:
    _queue(db, project, settings, f"Prodbat alerts for {project.domain} will show here",
           "You'll get one when something breaks or needs a look, and another when it's working again.",
           device=device)


# ---- sending ----------------------------------------------------------------------

def payload(message: PushMessage, project: Project, settings: Settings) -> bytes:
    """What the service worker (static/sw.js) gets. The tag makes a problem's
    later notifications replace its earlier one."""
    tag = f"prodbat-{message.incident_id}" if message.incident_id else f"prodbat-m{message.id}"
    return json.dumps({"title": message.title, "body": message.body, "tag": tag,
                       "url": f"{settings.base}/d/{project.page_token}",
                       "icon": f"{settings.base}/static/icon-192.png"}).encode()


async def _send(http: httpx.AsyncClient, settings: Settings, key, device: PushSubscription,
                message: PushMessage, body: bytes) -> int:
    headers = {"Authorization": vapid_header(settings, key, device.endpoint), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "TTL": "86400",
               "Urgency": "high" if message.urgent else "normal"}
    if message.incident_id:
        # A newer message about the same problem replaces one still waiting for the device.
        headers["Topic"] = f"incident-{message.incident_id}"
    response = await http.post(device.endpoint, content=encrypt(body, device.p256dh, device.auth), headers=headers)
    return response.status_code


async def send_pending_push(sessions, settings: Settings, http: httpx.AsyncClient) -> int:
    if not settings.push_configured:
        return 0
    key = private_key(settings)
    sent, dropped = 0, {}
    async with sessions() as db:
        messages = (await db.scalars(
            select(PushMessage).where(PushMessage.sent_at.is_(None), PushMessage.attempts < MAX_ATTEMPTS)
            .order_by(PushMessage.created_at, PushMessage.id).limit(50).with_for_update(skip_locked=True))).all()
        for message in messages:
            project = await db.get(Project, message.project_id)
            query = select(PushSubscription).where(PushSubscription.project_id == project.id)
            if message.subscription_id:
                query = query.where(PushSubscription.id == message.subscription_id)
            body = payload(message, project, settings)
            message.attempts += 1
            delivered, problems, gone = 0, [], []
            for device in (await db.scalars(query)).all():
                try:
                    status = await _send(http, settings, key, device, message, body)
                except Exception as exc:  # keep the rest moving
                    problems.append(f"{device.label}: {type(exc).__name__}: {exc}")
                    continue
                if status < 300:
                    delivered += 1
                    device.last_used_at = dt.datetime.now(dt.UTC)
                elif status in (404, 410):  # the browser dropped this address; it will never work again
                    gone.append(device)
                else:
                    problems.append(f"{device.label}: status {status}")
            if delivered or not problems:
                # Retrying would repeat it on the devices that already have it.
                message.sent_at, message.last_error = dt.datetime.now(dt.UTC), "; ".join(problems)[:500] or None
                sent += 1
            else:
                message.last_error = "; ".join(problems)[:500]
            for device in gone:
                dropped[device.id] = device
                project.push_error = (f"{device.label} stopped accepting notifications, so we removed it. Open "
                                      f"this page on it and press Notify me on this device to turn them on again.")
        if dropped:  # last, so no message still to be updated goes with them
            await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(list(dropped))))
        await db.commit()
    return sent
