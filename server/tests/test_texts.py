import re
import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from prodbat_server.app import create_app
from prodbat_server.detect import evaluate_all
from prodbat_server.models import TextMessage
from prodbat_server.texts import normalize_number, send_pending_texts

from .helpers import BROWSER, SETTINGS, failure, http_client, minutes_ago, page_text, run

TEXTING = SETTINGS.model_copy(update={"twilio_account_sid": "AC123", "twilio_auth_token": "tok",
                                      "twilio_from": "+15550001111"})


@pytest.fixture
def texting(db, site):
    with TestClient(create_app(TEXTING, http=http_client(site)), headers={"user-agent": BROWSER}) as client:
        yield client


def texts(db):
    db.expire_all()
    return db.scalars(select(TextMessage).order_by(TextMessage.id)).all()


def test_numbers_need_a_country_code():
    assert normalize_number("+1 (555) 123-4567") == "+15551234567"
    assert normalize_number("0044 7700 900123") == "+447700900123"
    assert normalize_number("555-1234") is None and normalize_number("call me") is None


def test_a_number_is_confirmed_with_a_texted_code(texting, client, db, project):
    assert "Text messages" not in page_text(client, project, "settings"), "not offered when the server can't send texts"
    assert "Text me a code" in page_text(texting, project, "settings")

    base = f"/d/{project.page_token}"
    assert "country code" in texting.post(f"{base}/sms", data={"number": "555 1234"}).text
    texting.post(f"{base}/sms", data={"number": "+1 (555) 123-4567"})
    code = re.search(r"your code is (\d{6})", texts(db)[-1].body)[1]
    assert texts(db)[-1].to == "+15551234567"
    assert "didn't match" in texting.post(f"{base}/sms/confirm", data={"code": "000000"}).text.replace("&#39;", "'")
    assert "Texts are on" in texting.post(f"{base}/sms/confirm", data={"code": code}).text
    db.refresh(project)
    assert project.sms_to == "+15551234567" and project.sms_pending is None
    texting.post(f"{base}/sms/remove")
    db.refresh(project)
    assert project.sms_to is None


def test_only_broken_alerts_and_their_recovery_are_texted(db, project):
    project.sms_to = "+15551234567"
    db.add_all([failure(project, v, minutes_ago(40)) for v in "ab"])  # two visitors a day: needs a look
    db.commit()
    run(lambda sessions: evaluate_all(sessions, TEXTING), db)
    assert texts(db) == []

    db.add_all([failure(project, v, minutes_ago(2)) for v in "cde"])  # three in 15 minutes: broken
    db.commit()
    run(lambda sessions: evaluate_all(sessions, TEXTING), db)
    sent = texts(db)
    assert len(sent) == 1 and sent[0].body.startswith("Prodbat. Broken: POST /api/save is failing on yourapp.com.")


def test_a_plan_without_texts_sends_none(db, project):
    project.sms_to, project.sms_enabled = "+15551234567", False
    db.add_all([failure(project, v, minutes_ago(2)) for v in "abc"])
    db.commit()
    run(lambda sessions: evaluate_all(sessions, TEXTING), db)
    assert texts(db) == []


def test_the_worker_sends_texts_through_twilio(db, project):
    db.add(TextMessage(project_id=project.id, to="+15551234567", body="Prodbat. Broken: test"))
    db.commit()
    seen = []

    def twilio(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SM1"})

    async def go(sessions):
        async with httpx.AsyncClient(transport=httpx.MockTransport(twilio)) as http:
            return await send_pending_texts(sessions, TEXTING, http)

    assert run(go, db) == 1
    form = dict(urllib.parse.parse_qsl(seen[0].content.decode()))
    assert str(seen[0].url) == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert form == {"To": "+15551234567", "Body": "Prodbat. Broken: test", "From": "+15550001111"}
    assert texts(db)[0].sent_at is not None


def test_texts_can_be_limited_to_some_countries(db, site, project):
    us_only = TEXTING.model_copy(update={"text_countries": "+1"})
    with TestClient(create_app(us_only, http=http_client(site)), headers={"user-agent": BROWSER}) as client:
        base = f"/d/{project.page_token}"
        assert "US numbers (+1)" in client.get(f"{base}/settings").text
        refused = client.post(f"{base}/sms", data={"number": "+44 7700 900123"}).text
        assert "Texts can only go to US numbers (+1)" in refused
        assert texts(db) == []
        client.post(f"{base}/sms", data={"number": "+1 555 123 4567"})
        assert texts(db)[-1].to == "+15551234567"
