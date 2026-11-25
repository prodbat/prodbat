import base64
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient
from sqlalchemy import select

from prodbat_server.app import create_app
from prodbat_server.detect import evaluate_all
from prodbat_server.models import Incident, PushMessage, PushSubscription
from prodbat_server.push import b64, label, new_vapid_keys, plain, send_pending_push, unb64

from .helpers import BROWSER, SETTINGS, failure, minutes_ago, page_text, run

PUBLIC, PRIVATE = new_vapid_keys()
PUSHING = SETTINGS.model_copy(update={"vapid_public_key": PUBLIC, "vapid_private_key": PRIVATE,
                                      "vapid_subject": "mailto:alerts@pb.example"})
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"


class Phone:
    """A browser's keys, so a test can read what was sent to it."""

    def __init__(self, n=1):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = f"https://fcm.googleapis.com/fcm/send/device-{n}"

    def subscription(self):
        public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "expirationTime": None, "keys": {"p256dh": b64(public), "auth": b64(self.auth)}}

    def read(self, body: bytes) -> dict:
        """RFC 8291 from the device's side."""
        salt, sender_public, record = body[:16], body[21:86], body[86:]
        assert int.from_bytes(body[16:20], "big") == 4096 and body[20] == 65
        public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
        ikm = HKDF(hashes.SHA256(), 32, self.auth, b"WebPush: info\x00" + public + sender_public).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        plaintext = AESGCM(cek).decrypt(nonce, record, None)
        assert plaintext.endswith(b"\x02")
        return json.loads(plaintext[:-1])


class PushService:
    """A stand-in for the browsers' push services: records requests, answers `status`."""

    def __init__(self):
        self.seen = []
        self.status = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(self.status.get(str(request.url), 201))


@pytest.fixture
def service():
    return PushService()


@pytest.fixture
def pushing(db, service):
    app = create_app(PUSHING, http=httpx.AsyncClient(transport=httpx.MockTransport(service)))
    with TestClient(app, headers={"user-agent": ANDROID}, follow_redirects=False) as client:
        yield client


def deliver(db, service):
    async def go(sessions):
        async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as http:
            return await send_pending_push(sessions, PUSHING, http)
    return run(go, db)


def messages(db):
    db.expire_all()
    return db.scalars(select(PushMessage).order_by(PushMessage.id)).all()


def devices(db):
    db.expire_all()
    return db.scalars(select(PushSubscription).order_by(PushSubscription.id)).all()


def test_turning_notifications_on_and_off(pushing, client, db, project, service):
    assert "Phone notifications" not in page_text(client, project, "settings"), "not offered without VAPID keys"
    page = pushing.get(f"/d/{project.page_token}/settings").text
    assert "Notify me on this device" in page and PUBLIC in page
    assert f'href="/d/{project.page_token}/manifest.webmanifest"' in page and "apple-touch-icon" in page

    phone = Phone()
    added = pushing.post(f"/d/{project.page_token}/push", json=phone.subscription())
    assert added.status_code == 200 and added.json()["label"] == "Chrome on Android"
    assert [d.label for d in devices(db)] == ["Chrome on Android"]
    assert pushing.post(f"/d/{project.page_token}/push", json=phone.subscription()).status_code == 200
    assert len(devices(db)) == 1, "the same browser again is the same device"
    assert len(messages(db)) == 1, "a hello goes to a new device only"

    assert deliver(db, service) == 1
    hello = phone.read(service.seen[0].content)
    assert hello["title"] == "Prodbat alerts for yourapp.com will show here"
    assert hello["url"] == f"https://pb.example/d/{project.page_token}"

    assert "Chrome on Android" in page_text(pushing, project, "settings")
    assert pushing.post(f"/d/{project.page_token}/test-alert").status_code == 303
    assert messages(db)[-1].title == "Test: a Prodbat alert for yourapp.com"
    assert "on its way to you@example.com and 1 device" in pushing.get(f"/d/{project.page_token}/settings?sent=test").text

    device = devices(db)[0]
    gone = pushing.post(f"/d/{project.page_token}/push/{device.id}/remove")
    assert gone.headers["location"] == f"/d/{project.page_token}/settings?sent=pushoff#push"
    assert devices(db) == []


def test_only_real_push_services_and_real_keys(pushing, db, project):
    phone = Phone().subscription()
    for endpoint in ("http://fcm.googleapis.com/x", "https://169.254.169.254/latest", "https://fcm.googleapis.com.evil.com/x"):
        assert pushing.post(f"/d/{project.page_token}/push", json=phone | {"endpoint": endpoint}).status_code == 400
    bad = phone | {"keys": {"p256dh": b64(b"\x04" + b"\x01" * 64), "auth": phone["keys"]["auth"]}}
    assert pushing.post(f"/d/{project.page_token}/push", json=bad).status_code == 400
    assert pushing.post(f"/d/{project.page_token}/push", json={"nope": 1}).status_code == 400
    assert pushing.post("/d/nosuchpage/push", json=phone).status_code == 404
    apple = phone | {"endpoint": "https://web.push.apple.com/QGx"}
    assert pushing.post(f"/d/{project.page_token}/push", json=apple).status_code == 200
    assert devices(db)[0].endpoint == "https://web.push.apple.com/QGx"


def test_an_alert_is_encrypted_and_signed(db, project, service):
    phone = Phone()
    db.add(PushSubscription(project_id=project.id, endpoint=phone.endpoint, label="Chrome on Android",
                            **phone.subscription()["keys"]))
    db.add_all([failure(project, v, minutes_ago(2)) for v in "abc"])
    run(lambda sessions: evaluate_all(sessions, PUSHING), db)
    assert [m.title for m in messages(db)] == ["Broken: POST /api/save is failing on yourapp.com"]
    assert deliver(db, service) == 1

    request = service.seen[0]
    assert str(request.url) == phone.endpoint
    assert request.headers["content-encoding"] == "aes128gcm" and request.headers["urgency"] == "high"
    assert int(request.headers["ttl"]) > 0
    incident = db.scalars(select(Incident)).one()
    assert request.headers["topic"] == f"incident-{incident.id}"
    shown = phone.read(request.content)
    assert shown["title"] == "Broken: POST /api/save is failing on yourapp.com"
    assert shown["tag"] == f"prodbat-{incident.id}" and shown["icon"] == "https://pb.example/static/icon-192.png"

    # The Authorization header is a VAPID token for this push service, signed with our key.
    scheme, _, rest = request.headers["authorization"].partition(" ")
    parts = dict(p.strip().split("=", 1) for p in rest.split(","))
    assert scheme == "vapid" and parts["k"] == PUBLIC
    head, claims, signature = parts["t"].split(".")
    assert json.loads(unb64(claims))["aud"] == "https://fcm.googleapis.com"
    assert json.loads(unb64(claims))["sub"] == "mailto:alerts@pb.example"
    raw = unb64(signature)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(PUBLIC))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert devices(db)[0].last_used_at is not None

    run(_recover, db)
    deliver(db, service)
    again = phone.read(service.seen[-1].content)
    assert again["title"] == "Working again: POST /api/save is failing on yourapp.com"
    assert again["tag"] == shown["tag"], "replaces the Broken one on the device"


async def _recover(sessions):
    from prodbat_server.incidents import resolve
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        incident = (await db.scalars(select(Incident))).one()
        resolve(db, project, incident, PUSHING, minutes_ago(0), recovery="POST /api/save works again.")
        await db.commit()


async def _report(sessions, what):
    from prodbat_server.incidents import report
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        await report(db, project, PUSHING, kind="test", level="look", title="Checkout is slow",
                     what=what, who="2 visitors.", started_at=minutes_ago(3))
        await db.commit()


def test_a_device_that_is_gone_is_removed(db, project, service, pushing):
    one, two = Phone(1), Phone(2)
    for phone in (one, two):
        db.add(PushSubscription(project_id=project.id, endpoint=phone.endpoint, label=f"Phone {phone.endpoint[-1]}",
                                **phone.subscription()["keys"]))
    run(lambda sessions: _report(sessions, "x"), db)
    service.status[one.endpoint] = 410
    assert deliver(db, service) == 1
    assert [d.endpoint for d in devices(db)] == [two.endpoint]
    assert messages(db)[0].sent_at is not None
    assert "Phone 1 stopped accepting notifications" in page_text(pushing, project, "settings")


def test_a_failing_push_service_is_tried_again(db, project, service):
    phone = Phone()
    db.add(PushSubscription(project_id=project.id, endpoint=phone.endpoint, label="Phone",
                            **phone.subscription()["keys"]))
    run(lambda sessions: _report(sessions, "x"), db)
    service.status[phone.endpoint] = 503
    assert deliver(db, service) == 0
    message = messages(db)[0]
    assert message.sent_at is None and message.attempts == 1 and "status 503" in message.last_error
    assert devices(db), "kept"
    service.status.clear()
    assert deliver(db, service) == 1
    assert service.seen[-1].headers["urgency"] == "normal", "a Needs a look is not urgent"


def test_a_plan_without_notifications_sends_nothing(db, project, pushing):
    project.push_enabled = False
    db.commit()
    assert "Phone notifications" not in page_text(pushing, project, "settings")
    assert pushing.post(f"/d/{project.page_token}/push", json=Phone().subscription()).status_code == 403
    run(lambda sessions: _report(sessions, "x"), db)
    assert messages(db) == []


def test_text_is_plain_and_short(db, project, service):
    phone = Phone()
    db.add(PushSubscription(project_id=project.id, endpoint=phone.endpoint, label="Phone",
                            **phone.subscription()["keys"]))
    run(lambda sessions: _report(sessions, "<b>card</b>\n\x07declined " + "word " * 200), db)
    deliver(db, service)
    body = phone.read(service.seen[0].content)["body"]
    assert body.startswith("<b>card</b> declined word") and len(body) <= 240 and body.endswith("…")
    assert plain("short", 10) == "short"
    assert label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1") == "Safari on iPhone"
    assert label(BROWSER) == "Safari on Mac"


def test_the_service_worker_and_manifest(pushing, project):
    worker = pushing.get("/d/sw.js")
    assert worker.status_code == 200 and "showNotification" in worker.text
    assert worker.headers["content-type"].startswith("text/javascript")
    manifest = pushing.get(f"/d/{project.page_token}/manifest.webmanifest").json()
    assert manifest["name"] == "Prodbat · yourapp.com" and manifest["display"] == "standalone"
    assert manifest["start_url"] == f"/d/{project.page_token}"
    assert pushing.get("/static/icon-192.png").headers["content-type"] == "image/png"
    assert pushing.get("/d/nosuchpage/manifest.webmanifest").status_code == 404
