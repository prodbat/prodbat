"""What the registries say about a package version is asked once and
shared by every site that uses it."""

from sqlalchemy import select, update

from prodbat_server.models import Dependency, EolFacts, Incident, PackageFacts, Project, hash_secret

from prodbat_server.worker import prune

from .helpers import SETTINGS, days_ago, run, run_jobs

PACKAGES = [
    {"ecosystem": "npm", "name": "request", "version": "2.88.2", "direct": True},
    {"ecosystem": "npm", "name": "inflight", "version": "1.0.6"},
    {"ecosystem": "npm", "name": "next", "version": "14.2.3", "direct": True},
]
OTHER = {"authorization": "Bearer pbs_other"}


def dev(name, version, deprecated=""):
    key = {"system": "NPM", "name": name, "version": version}
    return {"request": {"versionKey": key},
            "version": {"versionKey": key, "isDeprecated": bool(deprecated), "deprecatedReason": deprecated}}


def registries(site):
    site.osv = {
        "/v1/querybatch": {"results": [{}, {"vulns": [{"id": "GHSA-low"}]}, {"vulns": [{"id": "GHSA-next"}]}]},
        "/v1/vulns/GHSA-low": {"id": "GHSA-low", "summary": "Minor.", "database_specific": {"severity": "LOW"},
                               "affected": [{"package": {"name": "inflight"}, "ranges": [{"events": [{"fixed": "1.0.7"}]}]}]},
        "/v1/vulns/GHSA-next": {"id": "GHSA-next", "summary": "Cache poisoning in Next.js.",
                                "database_specific": {"severity": "CRITICAL"},
                                "affected": [{"package": {"name": "next"}, "ranges": [{"events": [{"fixed": "14.2.10"}]}]}]},
    }
    site.json = {
        "/v3alpha/versionbatch": {"responses": [dev("request", "2.88.2", "request has been deprecated"),
                                                dev("inflight", "1.0.6"), dev("next", "14.2.3")]},
        "/v3/systems/npm/packages/next": {"versions": [{"versionKey": {"version": "16.4.6"}, "isDefault": True}]},
        "/v3/systems/npm/packages/request": {"versions": [{"versionKey": {"version": "2.88.2"}, "isDefault": True}]},
        "/api/nextjs.json": [{"cycle": "16", "eol": False}, {"cycle": "14", "eol": "2030-10-26"}],
    }


def facts(db, name):
    db.expire_all()
    return db.scalars(select(PackageFacts).where(PackageFacts.name == name)).one()


def found(db, project):
    db.expire_all()
    deps = db.scalars(select(Dependency).where(Dependency.project_id == project.id)).all()
    return {d.name: (d.deprecated, d.latest, d.issues, d.facts_checked_at is not None) for d in deps}


def incidents(db, project):
    db.expire_all()
    return sorted((i.kind, i.key, i.title) for i in db.scalars(select(Incident).where(Incident.project_id == project.id)))


def other_site(db):
    other = Project(domain="othersite.com", key="pb_other", secret_hash=hash_secret("pbs_other"))
    db.add(other)
    db.commit()
    return other


def test_a_second_site_with_the_same_versions_asks_nobody(client, db, project, key, site):
    registries(site)
    client.put("/api/dependencies", headers=key, json={"packages": PACKAGES})
    run_jobs(db, site)
    assert any(r.startswith("api.osv.dev") for r in site.lookups)
    assert any(r.startswith("api.deps.dev") for r in site.lookups)
    assert "endoflife.date/api/nextjs.json" in site.lookups
    first = found(db, project)
    assert first["request"][0] == "request has been deprecated" and first["next"][1] == "16.4.6"
    assert first["inflight"][2] == ["GHSA-low: Minor (low, fixed in 1.0.7)"]

    other = other_site(db)
    site.lookups.clear()
    client.put("/api/dependencies", headers=OTHER, json={"packages": PACKAGES})
    run_jobs(db, site)
    assert site.lookups == []
    # The same facts, and the same problems raised for it.
    assert found(db, other) == first
    assert incidents(db, other) == incidents(db, project) == [
        ("advisory", "npm/next", "next has a serious security problem"),
        ("deprecated", "npm/request", "request is deprecated")]


def test_a_fact_past_its_time_is_asked_again_and_a_fresh_one_is_not(client, db, project, key, site):
    registries(site)
    client.put("/api/dependencies", headers=key, json={"packages": PACKAGES})
    run_jobs(db, site)
    inflight_checked = facts(db, "inflight").deprecated_checked_at

    # Eight days on, request's deprecation is due again (7 days); two days on,
    # next's newest version is (24 hours). Everything else is still fresh.
    db.execute(update(PackageFacts).where(PackageFacts.name == "request").values(deprecated_checked_at=days_ago(8)))
    db.execute(update(PackageFacts).where(PackageFacts.name == "next").values(latest_checked_at=days_ago(2)))
    project.advisories_checked_at = project.freshness_checked_at = days_ago(2)
    site.json["/v3/systems/npm/packages/next"]["versions"][0]["versionKey"]["version"] = "16.5.0"
    site.lookups.clear()
    run_jobs(db, site)
    assert site.lookups == ["api.deps.dev/v3alpha/versionbatch", "api.deps.dev/v3/systems/npm/packages/next"]
    assert facts(db, "request").deprecated_checked_at > days_ago(1)
    assert facts(db, "inflight").deprecated_checked_at == inflight_checked
    assert found(db, project)["next"][1] == "16.5.0"

    # End of life facts keep for 7 days, then are asked for again.
    db.execute(update(EolFacts).values(checked_at=days_ago(8)))
    project.freshness_checked_at = days_ago(2)
    site.lookups.clear()
    run_jobs(db, site)
    assert site.lookups == ["endoflife.date/api/nextjs.json"]


def test_facts_past_their_time_still_show_while_they_cannot_be_asked_again(client, db, project, key, site):
    registries(site)
    client.put("/api/dependencies", headers=key, json={"packages": PACKAGES})
    run_jobs(db, site)

    # deps.dev and OSV are down when the facts are due again.
    db.execute(update(PackageFacts).values(deprecated_checked_at=days_ago(8), advisories_checked_at=days_ago(2)))
    project.advisories_checked_at = project.freshness_checked_at = days_ago(2)
    del site.json["/v3alpha/versionbatch"]
    site.osv = {}
    run_jobs(db, site)
    rows = {r["name"]: r for g in client.get("/api/dependencies", headers=key).json()["groups"] for r in g["rows"]}
    assert rows["request"]["status"] == "deprecated"
    assert rows["inflight"]["notes"] == ["1 known security problem. Version 1.0.7 fixes it."]
    assert {i[1] for i in incidents(db, project)} == {"npm/next", "npm/request"}
    assert all(i.ended_at is None for i in db.scalars(select(Incident)))
    # Still due, so the next check asks again.
    assert facts(db, "request").deprecated_checked_at < days_ago(7)


def test_facts_no_site_uses_any_more_are_pruned(db, project):
    db.add(Dependency(project_id=project.id, ecosystem="npm", name="used", version="1.0.0"))
    db.add_all([PackageFacts(ecosystem="npm", name="used", version="1.0.0", advisories_checked_at=days_ago(30)),
                PackageFacts(ecosystem="npm", name="used", version="0.9.0", advisories_checked_at=days_ago(30)),
                PackageFacts(ecosystem="npm", name="dropped", version="1.0.0", advisories_checked_at=days_ago(1))])
    db.commit()
    run(lambda sessions: prune(sessions, SETTINGS), db)
    db.expire_all()
    assert sorted((f.name, f.version) for f in db.scalars(select(PackageFacts))) == [
        ("dropped", "1.0.0"), ("used", "1.0.0")]
