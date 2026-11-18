"""Security problems in every package a site depends on, from the public
OSV database (https://osv.dev), which gathers the advisories of every
language's registry in one place.

A problem that is rated high or critical and already has a fixed version
is told to the owner at once. Everything else (lower ratings, or no fix
yet) is kept on the package and listed, not alerted.
"""

import datetime as dt
import re

import httpx
from sqlalchemy import select

from .config import Settings
from .incidents import digest, report, resolve
from .models import Dependency, Incident, Project

OSV = "https://api.osv.dev/v1"
DEPS_DEV = "https://api.deps.dev/v3"
SERIOUS = {"HIGH", "CRITICAL"}
SERIOUS_SCORE = 7.0
MAX_LOOKUPS = 150

# Advisory id -> its details, and id -> its numeric score. Advisories do
# not change often, and the same ones come up across sites.
_details: dict[str, dict] = {}
_scores: dict[str, float | None] = {}


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:4])


def fixed_version(vuln: dict, name: str, current: str) -> str | None:
    """The lowest fixed version above the one in use."""
    fixes = []
    for affected in vuln.get("affected", []):
        if (affected.get("package") or {}).get("name", "").lower() != name.lower():
            continue
        for rng in affected.get("ranges", []):
            fixes += [e["fixed"] for e in rng.get("events", []) if "fixed" in e]
    above = sorted((f for f in fixes if version_key(f) > version_key(current)), key=version_key)
    return above[0] if above else None


async def rating(client: httpx.AsyncClient, vuln: dict) -> str:
    """critical, high, moderate, low or unrated."""
    label = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
    if label:
        return {"MEDIUM": "moderate"}.get(label, label.lower())
    # No label (common outside GitHub's own advisories): ask deps.dev for the score.
    vid = vuln["id"]
    if vid not in _scores:
        try:
            response = await client.get(f"{DEPS_DEV}/advisories/{vid}")
            _scores[vid] = response.json().get("cvss3Score") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            _scores[vid] = None
    score = _scores[vid]
    if not score:
        return "unrated"
    return "critical" if score >= 9 else "high" if score >= SERIOUS_SCORE else "moderate" if score >= 4 else "low"


async def find_problems(client: httpx.AsyncClient, deps: list[Dependency]):
    """Returns (serious, lesser): dependency id -> [(advisory, fixed version)]
    for the ones to tell the owner about, and dependency id -> sentences
    for the rest."""
    serious: dict[int, list[tuple[dict, str]]] = {}
    lesser: dict[int, list[str]] = {}
    lookups = 0
    for start in range(0, len(deps), 500):
        batch = deps[start:start + 500]
        response = await client.post(f"{OSV}/querybatch", json={"queries": [
            {"package": {"name": d.name, "ecosystem": d.ecosystem}, "version": d.version} for d in batch]})
        response.raise_for_status()
        for dep, result in zip(batch, response.json().get("results", []), strict=False):
            seen_aliases: set[str] = set()
            for ref in (result or {}).get("vulns", []):
                vid = ref["id"]
                if vid not in _details:
                    if lookups >= MAX_LOOKUPS:
                        continue
                    lookups += 1
                    detail = await client.get(f"{OSV}/vulns/{vid}")
                    detail.raise_for_status()
                    _details[vid] = detail.json()
                vuln = _details[vid]
                # The same problem is often listed under several ids; say it once.
                names = {vid, *vuln.get("aliases", [])}
                if names & seen_aliases:
                    continue
                seen_aliases |= names
                fix = fixed_version(vuln, dep.name, dep.version)
                rated = await rating(client, vuln)
                if rated.upper() in SERIOUS and fix:
                    serious.setdefault(dep.id, []).append((vuln, fix))
                else:
                    summary = (vuln.get("summary") or "a security problem").rstrip(".")
                    fixed = f"fixed in {fix}" if fix else "no fix yet"
                    lesser.setdefault(dep.id, []).append(f"{vid}: {summary} ({rated}, {fixed})")
    return serious, lesser


async def refresh_advisories(sessions, client: httpx.AsyncClient, settings: Settings) -> None:
    """Once a day per site, and straight after its package list changes."""
    now = dt.datetime.now(dt.UTC)
    async with sessions() as db:
        stale = (await db.scalars(select(Project).where(Project.paused_at.is_(None)).where(
            (Project.advisories_checked_at.is_(None)) | (Project.advisories_checked_at < now - dt.timedelta(days=1)))
            .limit(20))).all()
        for project in stale:
            project.advisories_checked_at = now
            deps = (await db.scalars(select(Dependency).where(
                Dependency.project_id == project.id, Dependency.ecosystem != "runtime"))).all()
            try:
                serious, lesser = await find_problems(client, deps) if deps else ({}, {})
            except (httpx.HTTPError, ValueError, KeyError):
                await db.commit()
                continue
            wrong = set()
            new: list = []
            for dep in deps:
                dep.issues = lesser.get(dep.id, [])[:10]
                if dep.id not in serious:
                    continue
                found = serious[dep.id]
                vuln = found[0][0]
                # The version to move to has to clear every serious problem, not only the first.
                fix = max((f for _, f in found), key=version_key)
                key = f"{dep.ecosystem}/{dep.name}"
                wrong.add(key)
                summary = (vuln.get("summary") or "a security problem").strip().rstrip(".")
                indirect = "" if dep.direct else " Your app does not name this package itself; another package brings it in."
                if len(found) == 1:
                    title = f"{dep.name} has a serious security problem"
                    affected = f"is affected by {vuln['id']}: {summary}. It is fixed in {fix}."
                else:
                    title = f"{dep.name} has {len(found)} serious security problems"
                    affected = (f"is affected by {len(found)} serious security problems, including {vuln['id']}: "
                                f"{summary}. Version {fix} fixes all of them.")
                await report(
                    db, project, settings, kind="advisory", key=key, level="look", title=title,
                    what=f"Your app uses {dep.name} {dep.version}, which {affected}{indirect}",
                    who="Nobody yet, as far as we can tell.",
                    todo=f"Ask your coding agent to upgrade {dep.name} to {fix} or later, check that nothing "
                         f"else breaks, and deploy.",
                    started_at=now, gather=new,
                    data={"ecosystem": dep.ecosystem, "name": dep.name, "version": dep.version, "to": fix,
                          "direct": dep.direct})
            digest(db, project, new, settings, "security problems in your packages")
            for incident in await db.scalars(select(Incident).where(
                    Incident.project_id == project.id, Incident.kind == "advisory", Incident.ended_at.is_(None))):
                if incident.key not in wrong:
                    resolve(db, project, incident, settings, now)
            await db.commit()
