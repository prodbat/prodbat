import html
import re
"""The dependencies table on the page and at GET /api/dependencies."""

import datetime as dt

from prodbat_server.dependencies import build
from prodbat_server.models import Dependency, Incident, Project

from .helpers import NOW, page_text, run_jobs, view

CHECKED = NOW - dt.timedelta(hours=1)


def dep(name, version, ecosystem="npm", direct=True, latest=None, deprecated=None, issues=(), checked=True):
    return Dependency(ecosystem=ecosystem, name=name, version=version, direct=direct, latest=latest,
                      deprecated=deprecated, issues=list(issues), facts_checked_at=CHECKED if checked else None)


def problem(kind, key, data, title="A problem"):
    return Incident(kind=kind, key=key, level="look", title=title, what="", who="", data=data, started_at=NOW)


def rows(table):
    return {r["name"]: r for g in table["groups"] for r in g["rows"]}


def test_the_worst_thing_known_about_a_package_is_its_status():
    project = Project(domain="yourapp.com", freshness_checked_at=CHECKED)
    deps = [
        # Deprecated, end of life and a serious advisory: the advisory wins.
        dep("express", "3.0.0", latest="5.1.0", deprecated="Use v5"),
        dep("lodash", "4.17.20", latest="4.17.21", issues=["GHSA-1: Prototype pollution (moderate, no fix yet)"]),
        dep("next", "13.5.0", latest="16.0.0"),
        dep("django", "4.2.11", ecosystem="PyPI", latest="5.2.8"),
        dep("request", "2.88.2", latest="2.88.2", deprecated="request has been deprecated"),
        dep("left-pad", "1.0.0", latest="1.3.0"),
        dep("react", "19.0.0", latest="19.0.0"),
        dep("brand-new", "1.0.0", checked=False),
        dep("python", "3.12.1", ecosystem="runtime", latest="3.12.8", checked=False),
    ]
    incidents = [
        problem("advisory", "npm/express", {"ecosystem": "npm", "name": "express", "version": "3.0.0", "to": "4.20.0",
                                            "count": 2}),
        problem("eol", "express", {"ecosystem": "npm", "package": "express", "name": "Express", "over": True}),
        problem("eol", "nextjs", {"ecosystem": "npm", "package": "next", "name": "Next.js", "move_to": "16",
                                  "over": True, "ends": "2025-10-26"}),
        problem("eol", "django", {"ecosystem": "PyPI", "package": "django", "name": "Django", "move_to": "5.2",
                                  "over": False, "ends": "2026-12-31"}),
        problem("advisory", "npm/gone", {"ecosystem": "npm", "name": "gone", "to": "2.0.0"}),  # not in the list
    ]
    found = rows(build(project, deps, incidents))
    status = {name: r["status"] for name, r in found.items()}
    assert status == {"express": "security", "lodash": "security", "next": "eol", "django": "eol_soon",
                      "request": "deprecated", "left-pad": "outdated", "react": "ok", "brand-new": "checking",
                      "python": "outdated"}  # the language is covered by the project's daily pass
    assert found["express"]["notes"][0] == "2 known security problems. Version 4.20.0 fixes all of them."
    assert any(n.startswith("Support ended") for n in found["express"]["notes"])
    assert found["lodash"]["notes"] == ["1 known security problem. No fix yet."]
    assert found["next"]["notes"] == ["Support ended on 26 Oct 2025, so new security problems will not be fixed. "
                                      "Move to Next.js 16."]
    assert found["django"]["notes"] == ["Support ends on 31 Dec 2026. Move to Django 5.2."]
    assert (found["django"]["status_label"], found["django"]["pill"]) == ("End of life soon", "look")
    assert (found["express"]["pill"], found["next"]["pill"], found["left-pad"]["pill"], found["react"]["pill"]) == (
        "broken", "broken", "muted", "working")
    assert found["python"]["display"] == "Python" and found["python"]["label"] == "language"


def test_the_language_comes_first_then_direct_packages_and_indirect_ones_only_with_a_problem():
    project = Project(domain="yourapp.com", freshness_checked_at=CHECKED)
    deps = [dep("node", "24.7.0", ecosystem="runtime", latest="24.9.0"),
            dep("zod", "3.0.0", latest="3.0.0"), dep("axios", "1.0.0", latest="1.7.0"),
            dep("inflight", "1.0.6", direct=False, deprecated="leaks memory"),
            dep("minimist", "1.2.5", direct=False, issues=["GHSA-2: Prototype pollution (low, fixed in 1.2.6)"]),
            *[dep(f"tiny-{n}", "1.0.0", direct=False) for n in range(900)],
            dep("waiting", "1.0.0", direct=False, checked=False)]
    incidents = [problem("runtime", "node", {"version": "24.7.0", "to": "24.9.0", "missed": 2})]
    table = build(project, deps, incidents)
    assert [g["level"] for g in table["groups"]] == ["runtime", "direct", "indirect"]
    runtime, direct, indirect = table["groups"]
    assert runtime["rows"][0]["status"] == "security"
    assert runtime["rows"][0]["notes"] == ["Missing 2 security releases. Version 24.9.0 has the fixes."]
    assert [r["name"] for r in direct["rows"]] == ["axios", "zod"]  # outdated before OK
    assert [r["name"] for r in indirect["rows"]] == ["minimist", "inflight"]  # problems only, worst first
    assert table["hidden_indirect"] == 901 and table["problems"] == 3 and table["total"] == 906
    assert table["hidden_note"] == ("901 more packages pulled in by other packages have no known problem. "
                                    "1 of them is still being looked up.")


def seed(db, project):
    project.dependencies_sent_at = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
    project.freshness_checked_at = CHECKED
    db.add(view(project, "a"))
    for d in [dep("node", "20.11.1", ecosystem="runtime", latest="20.19.0"),
              dep('<img src=x onerror="alert(1)">', "1.0.0<script>", latest="2.0.0"),
              dep("express", "4.17.1", latest="5.1.0"),
              dep("inflight", "1.0.6", direct=False, deprecated="This module is not supported, and leaks memory."),
              dep("ms", "2.1.3", direct=False)]:
        d.project_id = project.id
        db.add(d)
    db.add(Incident(project_id=project.id, kind="eol", key="nodejs", level="look", started_at=NOW,
                    title="Node.js 20 no longer gets security fixes", what="", who="",
                    data={"name": "Node.js", "version": "20.11.1", "line": "20", "move_to": "24",
                          "ecosystem": "runtime", "package": "node", "ends": "2026-04-30", "over": True}))
    db.add(Incident(project_id=project.id, kind="advisory", key="npm/express", level="look", started_at=NOW,
                    title="express has a serious security problem", what="", who="",
                    data={"ecosystem": "npm", "name": "express", "version": "4.17.1", "to": "4.20.0"}))
    db.commit()


def tab_text(client, project, **params):
    r = client.get(f"/d/{project.page_token}/dependencies", params=params)
    assert r.status_code == 200
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", r.text.split("</style>", 1)[-1])))


def test_the_dependencies_tab_shows_them_and_escapes_package_names(client, db, project, key):
    seed(db, project)
    r = client.get(f"/d/{project.page_token}/dependencies")
    assert '<img src=x onerror="alert(1)">' not in r.text and "1.0.0<script>" not in r.text
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in r.text and "1.0.0&lt;script&gt;" in r.text
    assert "The list was last sent" not in page_text(client, project)  # not on the overview
    text = tab_text(client, project)
    assert "Dependencies 3 of 5 need a look" in text
    assert "The list was last sent on 18 Nov 2026." in text
    assert ("Language Node.js language Support ended on 30 Apr 2026, so new security problems will not be fixed. "
            "Move to Node.js 24. 20.11.1 20.19.0 End of life") in text
    assert "express npm 1 known security problem. Version 4.20.0 fixes it. 4.17.1 5.1.0 Security problem" in text
    assert ("Packages pulled in by other packages inflight npm Its authors deprecated this version: “This module "
            "is not supported, and leaks memory”. 1.0.6 – Deprecated") in text
    assert text.index("Packages your app names express") < text.index("Outdated")  # problems first

    api = client.get("/api/dependencies", headers=key).json()
    assert [g["level"] for g in api["groups"]] == ["runtime", "direct", "indirect"]
    assert api["groups"][0]["rows"][0]["status"] == "eol" and api["hidden_indirect"] == 1
    assert api["sent_at"].startswith("2026-11-18")
    assert client.get("/api/dependencies").status_code == 401
    status = client.get("/api/status", headers=key).json()
    assert "Dependencies with a problem (3 of 5):" in status["text"]
    assert "- Node.js 20.11.1 (language, runtime): End of life." in status["text"]
    assert status["dependencies"]["problems"] == 3


def test_before_the_list_is_sent_the_page_says_how_to_send_it(client, db, project, key):
    db.add(view(project, "a"))
    db.commit()
    text = tab_text(client, project)
    assert "No package list yet. Your coding agent sends it with npx prodbat deps" in text
    assert client.get("/api/dependencies", headers=key).json() == {
        "sent_at": None, "total": 0, "problems": 0, "hidden_indirect": 0, "groups": []}

    # Sent, not yet looked up.
    client.put("/api/dependencies", headers=key, json={"packages": [
        {"ecosystem": "npm", "name": "next", "version": "15.0.0", "direct": True}]})
    text = tab_text(client, project)
    assert "The list was last sent on" in text and "next npm Being looked up." in text and "Checking" in text


def test_end_of_life_and_end_of_life_soon_come_from_the_daily_lookup(client, db, project, key, site):
    soon = (dt.date.today() + dt.timedelta(days=30))
    site.json = {"/api/nodejs.json": [{"cycle": "20", "eol": "2026-04-30"}, {"cycle": "24", "eol": "2028-04-30"}],
                 "/api/django.json": [{"cycle": "5.2", "eol": "2030-04-30"}, {"cycle": "4.2", "eol": soon.isoformat()}],
                 "/v3alpha/versionbatch": {"responses": []}}
    client.put("/api/runtime", headers=key, json={"language": "node", "version": "20.11.1"})
    client.put("/api/dependencies", headers=key, json={"packages": [
        {"ecosystem": "PyPI", "name": "django", "version": "4.2.11", "direct": True}]})

    run_jobs(db, site)
    found = {r["name"]: r for g in client.get("/api/dependencies", headers=key).json()["groups"] for r in g["rows"]}
    assert found["node"]["status"] == "eol"
    assert found["node"]["notes"][0].startswith("Support ended on 30 Apr 2026")
    assert found["django"]["status"] == "eol_soon"
    assert found["django"]["notes"] == [f"Support ends on {soon.day} {soon:%b} {soon.year}. Move to Django 5.2."]
