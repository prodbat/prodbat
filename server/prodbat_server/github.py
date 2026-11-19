"""Problems as GitHub issues, for coding agents that work from issues.

The owner presses "Connect GitHub" on their page and sees GitHub's "Authorize
Prodbat" screen (the App's own user authorization). GitHub sends them back to
/github/callback with a code and the one-time state we gave it. With the code,
and only during that request, we ask GitHub which installations of this App the
owner's GitHub account can use, and which repositories in each it can reach; the
token is then dropped. If there is none, they go on to install the App and come
back to the same callback. They pick one repository from what GitHub listed, so
an installation id typed into an address is never trusted on its own.

From then on each Broken problem (and, if they ask, each Needs a
look) opens one issue labelled `prodbat`. Later news about it is a comment on
that issue, and "Working again" comments and closes it. What happens next is
up to the owner: an agent that is assigned the issue, or mentioned in it, or
that watches the label.

The App signs a short token with its private key (RS256) and swaps it for an
installation token, which lasts an hour and is kept until a few minutes before
it runs out. Only Issues (read and write) and Metadata (read) are asked for.

Text that strangers wrote (error messages, routes, log lines) is escaped for
Markdown; an @ or # in it gets a zero-width joiner so it can't mention anyone,
wake an agent or link another issue, and it can't close the code fence it sits in.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import select, update

from .config import Settings
from .models import GitHubPost, Incident, Project

API = "https://api.github.com"
MAX_ATTEMPTS = 5
MAX_BODY = 60_000  # GitHub takes 65,536 characters
LABEL = "prodbat"
OAUTH = "https://github.com/login/oauth"
# Long enough to install the App on GitHub on the way.
STATE_LIFE = dt.timedelta(minutes=30)
# What the owner's GitHub account can reach is kept this long for them to pick from.
PICK_LIFE = dt.timedelta(minutes=10)
MAX_INSTALLATIONS = 20
# An issue for the same problem that was closed this recently is reopened instead
# of opening another, so a problem that comes and goes stays on one issue.
REOPEN_WITHIN = dt.timedelta(days=7)
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
ZWJ = "‍"
UNINSTALLED = "Prodbat's GitHub App was uninstalled, or lost access. Connect GitHub again to get issues."
NO_REPO = "Prodbat can no longer reach {repo}. The app may have lost access to it. Connect GitHub again and pick it."
NO_ISSUES = "Issues are turned off in {repo}. Turn them on in the repository's settings, then connect GitHub again."
NOT_YOURS = ("GitHub didn't list that installation for your GitHub account, so it wasn't connected. Connect GitHub "
             "again while signed in to GitHub as someone who can use it.")
NO_ANSWER = "GitHub didn't finish connecting. Nothing changed. Try again."
# After GitHub says to slow down, nothing is sent until this time.
_quiet_until = 0.0
# Installation tokens, by installation id: (token, when it stops being used).
_tokens: dict[int, tuple[str, float]] = {}


class Gone(Exception):
    """GitHub answered that the installation or repository is out of reach for good."""


class SlowDown(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"rate limited for {seconds:.0f}s")
        self.seconds = seconds


def can_connect(project: Project, settings: Settings) -> bool:
    return bool(settings.github_configured and project.github_enabled)


def can_post(project: Project, settings: Settings) -> bool:
    return bool(can_connect(project, settings) and project.paused_at is None and project.github_installation_id
                and project.github_repo and not project.github_error)


def wants(project: Project, incident: Incident) -> bool:
    return incident.level == "broken" or (incident.level == "look" and project.github_look)


# ---- escaping ---------------------------------------------------------------------

def neutral(value: str) -> str:
    """No mentions (@someone, @claude) and no issue links (#12, owner/repo#12)."""
    return re.sub(r"([@#])(?=[\w-])", lambda m: m.group(1) + ZWJ, value or "")


def md(value: str) -> str:
    """Stranger text as Markdown that shows exactly as written."""
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value or "")
    return neutral(re.sub(r"([\\`*_{}\[\]<>()#+\-.!|~=])", r"\\\1", value))


def fenced(lines: list[str]) -> str:
    """A code block whose fence is longer than any run of backticks inside, so nothing can close it."""
    text = neutral("\n".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", line) for line in lines))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def cut(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def clean_assignees(text: str) -> list[str] | None:
    """"Copilot, ann" as logins, or None when one isn't a GitHub login."""
    names = [n.strip().lstrip("@") for n in (text or "").split(",") if n.strip()]
    if len(names) > 10 or any(not LOGIN.fullmatch(n) for n in names):
        return None
    return names


def clean_mention(text: str) -> str | None:
    """One line, under 300 characters. It is the owner's own text, so it is kept as written."""
    text = " ".join((text or "").split())
    return text[:300] or None


# ---- the app's tokens -------------------------------------------------------------

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(settings: Settings, now: float | None = None) -> str:
    """Proves a request comes from the App: RS256, issued a minute ago for clock drift, good for nine minutes."""
    now = int(now or time.time())
    key = serialization.load_pem_private_key(settings.github_private_key_pem, password=None)
    signing = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode()) + "." + \
        b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(settings.github_app_id)},
                       separators=(",", ":")).encode())
    return signing + "." + b64(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))


HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Prodbat"}


def slow_down_for(response: httpx.Response) -> float | None:
    """Seconds to wait when GitHub's answer is a rate limit (primary or secondary), else None."""
    if response.status_code not in (403, 429):
        return None
    if response.headers.get("retry-after"):
        try:
            return max(1.0, float(response.headers["retry-after"]))
        except ValueError:
            return 60.0
    if response.headers.get("x-ratelimit-remaining") == "0" and response.headers.get("x-ratelimit-reset"):
        try:
            return max(1.0, float(response.headers["x-ratelimit-reset"]) - time.time())
        except ValueError:
            return 60.0
    if response.status_code == 429 or "rate limit" in response.text.lower():
        return 60.0  # GitHub's advice when it gives no time: wait at least a minute
    return None


async def installation_token(http: httpx.AsyncClient, settings: Settings, installation_id: int) -> str:
    cached = _tokens.get(installation_id)
    if cached and cached[1] > time.time():
        return cached[0]
    response = await http.post(f"{API}/app/installations/{installation_id}/access_tokens",
                               headers=HEADERS | {"Authorization": f"Bearer {app_jwt(settings)}"})
    if (wait := slow_down_for(response)) is not None:
        raise SlowDown(wait)
    if response.status_code in (401, 403, 404):
        raise Gone(UNINSTALLED)
    response.raise_for_status()
    answer = response.json()
    try:
        expires = dt.datetime.fromisoformat(answer["expires_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        expires = time.time() + 3600
    _tokens[installation_id] = (answer["token"], expires - 300)  # five minutes to spare
    return answer["token"]


async def call(http: httpx.AsyncClient, settings: Settings, project: Project, method: str, path: str,
               body: dict | None = None) -> httpx.Response:
    """One request as the installation. Rate limits raise SlowDown; a lost repo or installation raises Gone."""
    token = await installation_token(http, settings, project.github_installation_id)
    response = await http.request(method, f"{API}{path}", json=body,
                                  headers=HEADERS | {"Authorization": f"Bearer {token}"})
    if (wait := slow_down_for(response)) is not None:
        raise SlowDown(wait)
    if response.status_code == 401:
        _tokens.pop(project.github_installation_id, None)
        raise Gone(UNINSTALLED)
    if response.status_code in (403, 404):
        raise Gone(NO_REPO.format(repo=project.github_repo or "the repository"))
    if response.status_code == 410:
        raise Gone(NO_ISSUES.format(repo=project.github_repo or "the repository"))
    return response


# ---- connecting -------------------------------------------------------------------

def new_state(project: Project, now: dt.datetime | None = None) -> str:
    """A one-time state for this project: "<project id>.<random>". The random part is kept to check it."""
    project.github_state = secrets.token_urlsafe(24)
    project.github_state_at = now or dt.datetime.now(dt.UTC)
    return f"{project.id.hex}.{project.github_state}"


def callback_url(settings: Settings) -> str:
    return f"{settings.base}/github/callback"


def authorize_url(settings: Settings, state: str) -> str:
    """GitHub's "Authorize Prodbat" screen, for the App's own user authorization."""
    return f"{OAUTH}/authorize?" + urllib.parse.urlencode(
        {"client_id": settings.github_app_client_id, "state": state, "redirect_uri": callback_url(settings)})


def install_url(settings: Settings, state: str) -> str:
    """Install the App (or change its repositories). With "Request user authorization
    (OAuth) during installation" on, GitHub comes back to the callback with a code."""
    return f"https://github.com/apps/{settings.github_app_slug}/installations/new?" + \
        urllib.parse.urlencode({"state": state})


def same(a: str, b: str) -> bool:
    """Equal, compared in constant time (and safe for any characters a browser sends)."""
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def state_is_good(project: Project | None, nonce: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.UTC)
    return bool(project is not None and project.github_state and nonce and project.github_state_at is not None
                and same(project.github_state, nonce) and now - project.github_state_at <= STATE_LIFE)


class Refused(Exception):
    """What GitHub said, in a sentence for the owner's page."""


async def user_token(http: httpx.AsyncClient, settings: Settings, code: str) -> str:
    """Swap the code GitHub sent back for a user access token. It is used during
    this one request and never kept."""
    response = await http.post(f"{OAUTH}/access_token", headers={"Accept": "application/json", "User-Agent": "Prodbat"},
                               data={"client_id": settings.github_app_client_id,
                                     "client_secret": settings.github_app_client_secret,
                                     "code": code, "redirect_uri": callback_url(settings)})
    try:
        token = response.json().get("access_token") if response.status_code == 200 else None
    except ValueError:
        token = None
    if not token:
        raise Refused(NO_ANSWER)
    return token


async def _as_user(http: httpx.AsyncClient, token: str, path: str, **params) -> dict:
    response = await http.get(f"{API}{path}", params=params or None,
                              headers=HEADERS | {"Authorization": f"Bearer {token}"})
    if slow_down_for(response) is not None:
        raise Refused("GitHub asked us to slow down. Try again in a minute.")
    if response.status_code != 200:
        raise Refused(NO_ANSWER)
    return response.json()


async def _pages(http: httpx.AsyncClient, token: str, path: str, key: str, pages: int) -> list[dict]:
    found = []
    for page in range(1, pages + 1):
        some = (await _as_user(http, token, path, per_page=100, page=page)).get(key) or []
        found += some
        if len(some) < 100:
            break
    return found


async def what_the_user_can_reach(http: httpx.AsyncClient, settings: Settings, token: str) -> dict:
    """With the owner's own token: their login, and each installation of this App
    they can use, with the repositories in it they can reach. GitHub decides both;
    nothing the browser sent is taken on trust."""
    login = (await _as_user(http, token, "/user")).get("login")
    installations = []
    for found in await _pages(http, token, "/user/installations", "installations", 5):
        if str(found.get("app_id")) != str(settings.github_app_id) or \
                (found.get("app_slug") and found["app_slug"] != settings.github_app_slug):
            continue  # another App's installation
        if not isinstance(found.get("id"), int) or len(installations) >= MAX_INSTALLATIONS:
            continue
        repos = await _pages(http, token, f"/user/installations/{found['id']}/repositories", "repositories", 3)
        installations.append({
            "id": found["id"], "account": (found.get("account") or {}).get("login") or "",
            "repos": sorted(({"name": r["full_name"], "private": bool(r.get("private"))}
                             for r in repos if r.get("full_name")), key=lambda r: r["name"].lower())[:300]})
    return {"login": login, "installations": installations}


def hold_choices(project: Project, reach: dict, now: dt.datetime | None = None) -> str:
    """Keep what the owner can pick from for a few minutes. Returns the nonce the
    picking browser must show again (in a cookie)."""
    nonce = secrets.token_urlsafe(24)
    project.github_pending = {"nonce": nonce, "at": (now or dt.datetime.now(dt.UTC)).isoformat(),
                              "login": reach.get("login"), "installations": reach["installations"]}
    return nonce


def choices(project: Project, now: dt.datetime | None = None) -> dict | None:
    """What the owner can pick from, while it lasts."""
    pending = project.github_pending
    if not pending:
        return None
    try:
        at = dt.datetime.fromisoformat(pending["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if (now or dt.datetime.now(dt.UTC)) - at > PICK_LIFE:
        return None
    return pending


async def pick(db, project: Project, nonce: str, choice: str) -> bool:
    """Connect the repository the owner chose, if GitHub listed it for them, from
    the browser that went to GitHub."""
    pending = choices(project)
    if pending is None or not nonce or not pending.get("nonce") or not same(str(pending["nonce"]), nonce):
        return False
    installation_id, _, repo = choice.partition(":")
    for installation in pending.get("installations") or []:
        if str(installation["id"]) == installation_id and repo in [r["name"] for r in installation["repos"]]:
            await connect(db, project, installation, repo, pending.get("login"))
            return True
    return False


async def connect(db, project: Project, installation: dict, repo: str, login: str | None) -> None:
    if project.github_installation_id != installation["id"] or project.github_repo != repo:
        await forget_issues(db, project)
    project.github_installation_id = installation["id"]
    project.github_account = installation.get("account") or None
    project.github_repos = installation.get("repos") or []
    project.github_repo = repo
    project.github_login = login
    project.github_pending = None
    project.github_error = project.github_note = None


async def forget_issues(db, project: Project) -> None:
    """Issue numbers belong to one repository; in another they would point at the wrong issue."""
    if db is not None:
        await db.execute(update(Incident).where(Incident.project_id == project.id)
                         .values(github_issue=None, github_said=None))
    project.github_note = None


async def disconnect(db, project: Project) -> None:
    """Forget the connection. The app stays installed on GitHub until the owner removes it there."""
    await forget_issues(db, project)
    project.github_installation_id = project.github_account = project.github_repo = None
    project.github_state = project.github_state_at = project.github_error = project.github_note = None
    project.github_login = project.github_pending = None
    project.github_repos = []


# ---- what is queued ---------------------------------------------------------------

def _queue(db, project: Project, settings: Settings, action: str, incident: Incident | None = None,
           text: str = "") -> bool:
    if not can_post(project, settings):
        return False
    db.add(GitHubPost(project_id=project.id, incident=incident, action=action, text=text))
    return True


def post_alert(db, project: Project, incident: Incident, settings: Settings) -> None:
    """Open its issue, or, if one is already open for it, say what changed there."""
    if wants(project, incident):
        _queue(db, project, settings, "alert", incident)


def post_recovery(db, project: Project, incident: Incident, settings: Settings, recovery: str | None,
                  when: str) -> None:
    if incident.github_issue or wants(project, incident):
        said = f"Checked from outside: working again at {when}." if recovery else \
            f"Prodbat closed this at {when}: it no longer sees the problem, or it was marked as fine."
        _queue(db, project, settings, "close", incident, said + (f"\n\n{md(recovery)}" if recovery else ""))


def post_check(db, project: Project, incident: Incident | None, settings: Settings, task_id: str,
               result: dict) -> None:
    """A fix check's answer, as a comment on the problem's issue. The same words
    twice in a row are posted once (see `_send`)."""
    if incident is None or not (incident.github_issue or wants(project, incident)):
        return
    text = (result.get("text") or "").strip()
    head = {True: f"Prodbat checked task {task_id} from outside: fixed.",
            False: f"Prodbat checked task {task_id} from outside: still failing."}.get(
        result.get("fixed"), f"Prodbat checked task {task_id} from outside: not yet.")
    _queue(db, project, settings, "comment", incident, f"{head}\n\n{md(text)}" if text else head)


def post_test(db, project: Project, settings: Settings) -> bool:
    return _queue(db, project, settings, "test")


# ---- what an issue says -------------------------------------------------------------

def title_for(project: Project, incident: Incident) -> str:
    from .incidents import PREFIX

    return neutral(cut(f"{PREFIX.get(incident.level, 'Problem')}: {incident.title} on {project.domain}", 250))


async def issue_body(db, project: Project, incident: Incident, settings: Settings) -> str:
    from .webhooks import UNTRUSTED, describe

    facts, task, evidence = await describe(db, project, incident, settings, evidence_lines=60)
    plain = facts["plain"]
    since = f"{incident.started_at:%Y-%m-%d %H:%M} UTC" if incident.started_at else "unknown"
    head = [f"**{facts['label']}** on {md(project.domain)} since {since}.", "",
            f"**What broke:** {md(plain['what'])}", f"**Who is affected:** {md(plain['who'])}",
            f"**What to do:** {md(plain['todo'])}", "",
            "### For engineers", f"- Kind: `{incident.kind}`"]
    if facts["route"]:
        head.append(f"- Route: {md(str(facts['route']))}")
    for name, value in list((incident.data or {}).items())[:12]:
        if name not in ("route", "method") and isinstance(value, (str, int, float, bool)):
            head.append(f"- {md(str(name))}: {md(cut(str(value), 200))}")
    if task:
        head += ["", f"### Task {task['id']} for your coding agent", md(task["why"]), "",
                 *[f"{n}. {md(step)}" for n, step in enumerate(task["steps"], 1)], "",
                 md(task["done_when"]), "",
                 "Prompt for a coding agent:", "", f"> {md(task['agent_prompt'])}", "",
                 f"The full task and its evidence: run `npx prodbat next` (or `npx prodbat task {task['id']}`), or "
                 "use the Prodbat MCP tools. After deploying the fix, run "
                 f"`npx prodbat done {task['id']}` or call `prodbat_check_task`. Prodbat checks from outside and "
                 "comments here."]
    tail = ["", "---", f"<sub>Opened by Prodbat for {md(project.domain)}. Set up: {md(settings.base)} "
                       f"· [How agents use these issues]({DOCS_LINK})</sub>"]
    if project.github_mention:
        tail += ["", project.github_mention]
    tail.append(f"<!-- prodbat:incident:{incident.id} -->")

    def compose(lines: list[str]) -> str:
        block = ["", "### Evidence: untrusted data from outside", md(UNTRUSTED), "", fenced(lines)] if lines else []
        return "\n".join(head + block + tail)

    body = compose(evidence)
    while len(body) > MAX_BODY and evidence:
        evidence = evidence[:-1]
        body = compose(evidence + ["(more evidence was cut to fit)"] if evidence else [])
    return body[:MAX_BODY]


DOCS_LINK = "https://github.com/prodbat/prodbat/blob/main/docs/cloud-agents.md"


def test_body(project: Project, settings: Settings) -> str:
    return "\n".join([
        f"**Test from Prodbat.** Nothing is wrong with {md(project.domain)}. This issue is closed straight away.", "",
        "A real issue looks like this:", "",
        "**What broke:** POST /api/checkout answered with an error \\(500\\) for 4 visitors in the last 15 minutes.",
        "**Who is affected:** 4 visitors so far.",
        "**What to do:** Paste the line in the alert into your coding agent, or roll back your last deploy.", "",
        "It also has the task for a coding agent, the evidence in a fenced block, and, if you set them, your "
        "mention line and assignees. They are left off this test so no agent starts work on it.", "",
        "<!-- prodbat:test -->"])


# ---- sending ----------------------------------------------------------------------

def _said(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


async def _ensure_label(http, settings, project) -> None:
    response = await call(http, settings, project, "POST", f"/repos/{project.github_repo}/labels",
                          {"name": LABEL, "color": "15181B", "description": "Opened by Prodbat"})
    if response.status_code not in (201, 422):  # 422: it is already there
        response.raise_for_status()


async def _open(http, settings, project: Project, title: str, body: str, assignees: list[str]) -> int:
    """Create the issue. If GitHub refuses the label or an assignee (422), try without it and note that."""
    await _ensure_label(http, settings, project)
    payload = {"title": title, "body": body, "labels": [LABEL]}
    if assignees:
        payload["assignees"] = assignees
    response = await call(http, settings, project, "POST", f"/repos/{project.github_repo}/issues", payload)
    if response.status_code == 422 and "assignees" in payload:
        payload.pop("assignees")
        project.github_note = (f"GitHub wouldn't assign {', '.join(assignees)} in {project.github_repo}, so "
                               "issues are opened without assignees. Check the names, and that each can be "
                               "assigned there.")
        response = await call(http, settings, project, "POST", f"/repos/{project.github_repo}/issues", payload)
    if response.status_code == 422:
        payload.pop("labels")
        project.github_note = f"GitHub wouldn't add the {LABEL} label in {project.github_repo}, so issues go without it."
        response = await call(http, settings, project, "POST", f"/repos/{project.github_repo}/issues", payload)
    response.raise_for_status()
    return response.json()["number"]


async def _comment(http, settings, project: Project, incident: Incident, text: str) -> None:
    if incident.github_said == _said(text):
        return  # the same words were the last thing posted there
    response = await call(http, settings, project, "POST",
                          f"/repos/{project.github_repo}/issues/{incident.github_issue}/comments", {"body": text})
    response.raise_for_status()
    incident.github_said = _said(text)


async def _set_state(http, settings, project: Project, number: int, state: str) -> None:
    body = {"state": state} | ({"state_reason": "completed"} if state == "closed" else {})
    response = await call(http, settings, project, "PATCH", f"/repos/{project.github_repo}/issues/{number}", body)
    response.raise_for_status()


async def _earlier_issue(db, project: Project, incident: Incident) -> int | None:
    """The issue of the same problem, if it came back within a week."""
    return await db.scalar(select(Incident.github_issue).where(
        Incident.project_id == project.id, Incident.kind == incident.kind, Incident.key == incident.key,
        Incident.id != incident.id, Incident.github_issue.is_not(None),
        Incident.ended_at >= dt.datetime.now(dt.UTC) - REOPEN_WITHIN).order_by(Incident.ended_at.desc()).limit(1))


async def _send(db, http, settings: Settings, project: Project, post: GitHubPost) -> None:
    incident = post.incident
    if post.action == "test":
        number = await _open(http, settings, project,
                             f"Test from Prodbat: what an issue for {project.domain} looks like (closed straight away)",
                             test_body(project, settings), [])
        await _set_state(http, settings, project, number, "closed")
        return
    if incident is None:
        return
    from .incidents import PREFIX

    if post.action == "alert":
        if incident.github_issue is None and (earlier := await _earlier_issue(db, project, incident)):
            incident.github_issue = earlier
            await _set_state(http, settings, project, earlier, "open")
            await _comment(http, settings, project, incident,
                           f"**{PREFIX.get(incident.level)} again** since {incident.started_at:%Y-%m-%d %H:%M} UTC. "
                           f"This is now task T{incident.id}: run `npx prodbat done T{incident.id}` after the fix.\n\n"
                           f"{md(incident.what)}")
        elif incident.github_issue is None:
            incident.github_issue = await _open(
                http, settings, project, title_for(project, incident),
                await issue_body(db, project, incident, settings),
                clean_assignees(project.github_assignees or "") or [])
        else:
            await _comment(http, settings, project, incident,
                           f"**Now {PREFIX.get(incident.level)}:** {md(incident.title)}\n\n{md(incident.what)}\n\n"
                           f"**Who is affected:** {md(incident.who)}")
        return
    if incident.github_issue is None:
        return  # its issue was never opened (Needs a look issues are off, or opening failed)
    await _comment(http, settings, project, incident, post.text)
    if post.action == "close":
        await _set_state(http, settings, project, incident.github_issue, "closed")


async def send_pending_github(sessions, settings: Settings, http: httpx.AsyncClient) -> int:
    global _quiet_until
    if not settings.github_configured or time.monotonic() < _quiet_until:
        return 0
    sent = 0
    async with sessions() as db:
        posts = (await db.scalars(
            select(GitHubPost).where(GitHubPost.sent_at.is_(None), GitHubPost.attempts < MAX_ATTEMPTS)
            .order_by(GitHubPost.created_at, GitHubPost.id).limit(30).with_for_update(skip_locked=True))).all()
        for post in posts:
            project = await db.get(Project, post.project_id)
            await db.refresh(post, ["incident"])
            if not project.github_installation_id or not project.github_repo or project.github_error:
                post.attempts, post.last_error = MAX_ATTEMPTS, "GitHub is not connected"
                continue
            post.attempts += 1
            try:
                await _send(db, http, settings, project, post)
                post.sent_at, post.last_error = dt.datetime.now(dt.UTC), None
                sent += 1
            except SlowDown as exc:
                post.attempts -= 1
                _quiet_until = time.monotonic() + exc.seconds
                break
            except Gone as exc:
                post.attempts, post.last_error = MAX_ATTEMPTS, str(exc)[:500]
                project.github_error = str(exc)
            except Exception as exc:  # keep the rest moving
                post.last_error = f"{type(exc).__name__}: {exc}"[:500]
        await db.commit()
    return sent
