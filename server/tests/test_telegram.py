import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from prodbat_server import telegram
from prodbat_server.app import create_app
from prodbat_server.detect import evaluate_all
from prodbat_server.models import Incident, TelegramMessage
from prodbat_server.telegram import esc, send_pending_telegram

from .helpers import BROWSER, SETTINGS, failure, minutes_ago, page_text, run

CHATTING = SETTINGS.model_copy(update={"telegram_bot_token": "123:abc", "telegram_bot_name": "ProdbatBot",
                                       "telegram_webhook_secret": "hook-secret"})
SECRET = {"x-telegram-bot-api-secret-token": "hook-secret"}


class Telegram:
    """A stand-in for api.telegram.org: records sendMessage calls; `answers` replaces the next answers."""

    def __init__(self):
        self.seen = []
        self.answers = []
        self.message_id = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        assert request.url.path.startswith("/bot123:abc/")
        if self.answers:
            status, body = self.answers.pop(0)
            return httpx.Response(status, json=body)
        self.message_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.message_id}})

    def sent(self):
        return [json.loads(r.content) for r in self.seen if r.url.path.endswith("/sendMessage")]


@pytest.fixture
def bot():
    return Telegram()


@pytest.fixture(autouse=True)
def not_quiet():
    telegram._quiet_until = 0.0


@pytest.fixture
def chatting(db, bot):
    app = create_app(CHATTING, http=httpx.AsyncClient(transport=httpx.MockTransport(bot)))
    with TestClient(app, headers={"user-agent": BROWSER}, follow_redirects=False) as client:
        yield client


def messages(db):
    db.expire_all()
    return db.scalars(select(TelegramMessage).order_by(TelegramMessage.id)).all()


def deliver(db, bot):
    async def go(sessions):
        async with httpx.AsyncClient(transport=httpx.MockTransport(bot)) as http:
            return await send_pending_telegram(sessions, CHATTING, http)
    return run(go, db)


def start(text, chat=None):
    chat = chat or {"id": 4242, "type": "private", "first_name": "Ann", "last_name": "Lee"}
    return {"update_id": 1, "message": {"message_id": 7, "chat": chat, "text": text}}


def connect(db, project):
    project.telegram_chat_id, project.telegram_chat = 4242, "Ann Lee"
    db.commit()


def test_connecting_a_chat(chatting, client, db, project):
    assert "Connect Telegram" not in page_text(client, project), "not offered when the server has no bot"
    assert "Connect Telegram" in page_text(chatting, project)

    assert chatting.post(f"/d/{project.page_token}/telegram").status_code == 303
    db.refresh(project)
    code = project.telegram_code
    page = chatting.get(f"/d/{project.page_token}").text
    assert f"https://t.me/ProdbatBot?start={code}" in page and f"https://t.me/ProdbatBot?startgroup={code}" in page

    assert chatting.post("/telegram/webhook", json=start(f"/start {code}")).status_code == 403, "no secret"
    wrong = {"x-telegram-bot-api-secret-token": "nope"}
    assert chatting.post("/telegram/webhook", json=start(f"/start {code}"), headers=wrong).status_code == 403
    db.refresh(project)
    assert project.telegram_chat_id is None

    answer = chatting.post("/telegram/webhook", json=start(f"/start {code}"), headers=SECRET).json()
    assert answer == {"method": "sendMessage", "chat_id": 4242,
                      "text": "Connected. Prodbat alerts for yourapp.com will arrive here."}
    db.refresh(project)
    assert (project.telegram_chat_id, project.telegram_chat, project.telegram_code) == (4242, "Ann Lee", None)

    again = chatting.post("/telegram/webhook", json=start(f"/start {code}", {"id": 666, "type": "private"}),
                          headers=SECRET).json()
    assert "run out or was already used" in again["text"], "a code works once"
    db.refresh(project)
    assert project.telegram_chat_id == 4242

    assert "Alerts also go to Ann Lee in Telegram" in page_text(chatting, project)
    assert chatting.post(f"/d/{project.page_token}/test-alert").status_code == 303
    assert messages(db)[-1].text.startswith("<b>Test: this is what a Prodbat alert")
    assert "on its way to you@example.com and Ann Lee in Telegram" in \
        chatting.get(f"/d/{project.page_token}?sent=test").text

    gone = chatting.post(f"/d/{project.page_token}/telegram/remove")
    assert gone.headers["location"] == f"/d/{project.page_token}?sent=telegramoff#telegram"
    db.refresh(project)
    assert project.telegram_chat_id is None and project.telegram_chat is None


def test_a_group_and_codes_that_dont_work(chatting, db, project):
    chatting.post(f"/d/{project.page_token}/telegram")
    db.refresh(project)
    code = project.telegram_code
    hook = lambda update: chatting.post("/telegram/webhook", json=update, headers=SECRET).json()  # noqa: E731

    assert "run out or was already used" in hook(start("/start nosuchcode"))["text"]
    assert "press Connect Telegram" in hook(start("/start"))["text"]
    assert hook(start(f"/start@OtherBot {code}")) == {}, "meant for another bot"
    assert hook(start("hello there")) == {}, "everything else is ignored"
    assert hook({"update_id": 2, "edited_message": {"text": f"/start {code}"}}) == {}
    assert chatting.post("/telegram/webhook", content=b"not json", headers=SECRET).json() == {}

    project.telegram_code_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    db.commit()
    assert "run out" in hook(start(f"/start {code}"))["text"]
    assert f"start={code}" not in chatting.get(f"/d/{project.page_token}").text, "an old link is not shown"

    chatting.post(f"/d/{project.page_token}/telegram")
    db.refresh(project)
    group = {"id": -100123, "type": "supergroup", "title": "Ops <team>"}
    answer = hook(start(f"/start@prodbatbot {project.telegram_code}", group))
    assert answer["chat_id"] == -100123 and answer["text"].startswith("Connected.")
    db.refresh(project)
    assert (project.telegram_chat_id, project.telegram_chat) == (-100123, "Ops <team>")
    assert "Alerts also go to Ops <team> in Telegram" in page_text(chatting, project)
    assert "Ops &lt;team&gt;" in chatting.get(f"/d/{project.page_token}").text


def test_unconfigured_or_plan_disabled(client, chatting, db, project):
    assert client.post("/telegram/webhook", json=start("/start x"), headers=SECRET).status_code == 404
    project.telegram_enabled = False
    db.commit()
    assert "Connect Telegram" not in page_text(chatting, project)
    assert chatting.post(f"/d/{project.page_token}/telegram").status_code == 403
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    assert messages(db) == []


def test_a_problem_is_one_message_and_replies(db, project, bot):
    connect(db, project)
    db.add_all([failure(project, v, minutes_ago(2)) for v in "abc"])
    run(lambda sessions: evaluate_all(sessions, CHATTING), db)
    assert len(messages(db)) == 1
    assert deliver(db, bot) == 1

    first = bot.sent()[0]
    assert first["chat_id"] == 4242 and first["parse_mode"] == "HTML"
    assert first["link_preview_options"] == {"is_disabled": True} and "reply_parameters" not in first
    assert first["text"].startswith("<b>Broken: POST /api/save is failing on yourapp.com</b>")
    assert f'<a href="https://pb.example/d/{project.page_token}">Open your Prodbat page</a>' in first["text"]
    incident = db.scalars(select(Incident)).one()
    assert incident.telegram_message_id == 1

    run(_recover, db)
    deliver(db, bot)
    reply = bot.sent()[-1]
    assert reply["reply_parameters"] == {"message_id": 1, "allow_sending_without_reply": True}
    assert reply["text"].startswith("<b>Working again: POST /api/save is failing on yourapp.com</b>")


async def _recover(sessions):
    from prodbat_server.incidents import resolve
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        incident = (await db.scalars(select(Incident))).one()
        resolve(db, project, incident, CHATTING, minutes_ago(0), recovery="POST /api/save works again.")
        await db.commit()


async def _report(sessions, what, key=""):
    from prodbat_server.incidents import report
    from prodbat_server.models import Project
    async with sessions() as db:
        project = (await db.scalars(select(Project))).one()
        await report(db, project, CHATTING, kind="test", key=key, level="broken", title="Checkout is failing",
                     what=what, who="2 visitors.", started_at=minutes_ago(3))
        await db.commit()


def test_strangers_cannot_add_markup_or_mentions(db, project):
    connect(db, project)
    assert esc("<b>x</b> & @admin") == "&lt;b&gt;x&lt;/b&gt; &amp; @⁠admin"
    run(lambda sessions: _report(sessions, '<a href="https://evil">click</a> @channel </pre>'), db)
    text = messages(db)[0].text
    assert "<a href=\"https://evil\">" not in text and "&lt;a href=\"https://evil\"&gt;click&lt;/a&gt;" in text
    assert "@channel" not in text and "@⁠channel" in text
    assert text.count("<pre>") == 1 and text.count("</pre>") == 1


def test_a_blocked_bot_stops_and_says_so(db, project, bot, chatting):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    bot.answers.append((403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}))
    assert deliver(db, bot) == 0
    db.refresh(project)
    assert project.telegram_error == telegram.BLOCKED and project.telegram_chat_id is None
    assert messages(db)[0].attempts == 5, "not retried"
    page = page_text(chatting, project)
    assert "Prodbat's bot was blocked" in page and "Connect Telegram again" in page
    run(lambda sessions: _report(sessions, "y", key="other"), db)
    assert len(messages(db)) == 1, "nothing more is queued until it is connected again"


def test_a_deleted_chat_disconnects(db, project, bot):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    bot.answers.append((400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}))
    deliver(db, bot)
    db.refresh(project)
    assert project.telegram_error == telegram.NO_CHAT


def test_slow_down_stops_the_batch(db, project, bot):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x", key="a"), db)
    run(lambda sessions: _report(sessions, "y", key="b"), db)
    bot.answers.append((429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
                              "parameters": {"retry_after": 7}}))
    assert deliver(db, bot) == 0
    assert len(bot.seen) == 1, "the rest wait"
    assert [m.attempts for m in messages(db)] == [0, 0]
    assert deliver(db, bot) == 0 and len(bot.seen) == 1, "and nothing is sent until the wait is over"
    telegram._quiet_until = 0.0
    assert deliver(db, bot) == 2


def test_a_group_that_became_a_supergroup_moves_with_it(db, project, bot):
    connect(db, project)
    run(lambda sessions: _report(sessions, "x"), db)
    bot.answers.append((400, {"ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded",
                              "parameters": {"migrate_to_chat_id": -100999}}))
    assert deliver(db, bot) == 1
    db.refresh(project)
    assert project.telegram_chat_id == -100999 and bot.sent()[-1]["chat_id"] == -100999


def test_registering_the_webhook():
    import asyncio

    bot = Telegram()
    bot.answers.append((200, {"ok": True, "result": True, "description": "Webhook was set"}))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(bot)) as http:
            return await telegram.register_webhook(http, CHATTING)
    assert asyncio.run(go()) == "Webhook was set"
    sent = json.loads(bot.seen[0].content)
    assert bot.seen[0].url.path == "/bot123:abc/setWebhook"
    assert sent == {"url": "https://pb.example/telegram/webhook", "secret_token": "hook-secret",
                    "allowed_updates": ["message"]}
