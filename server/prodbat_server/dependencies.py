"""The dependencies table: every package the site runs on, the version in
use, the newest one, and one status for each.

Nothing here looks anything up. It reads what the worker already found:
the open "advisory", "eol" and "runtime" problems (advisories.py and
freshness.py), and what is kept on each package (deprecated, the lesser
security problems in `issues`, the newest version).

The language comes first, then the packages the app names itself, then
the ones other packages pull in. A lockfile can list 900 of those, so
only the ones with a problem are shown, and the rest are counted.
"""

import datetime as dt
import re

from sqlalchemy import select

from .advisories import version_key
from .freshness import PRODUCTS, SYSTEMS, as_date
from .models import Dependency, Incident, Project
from .words import day, plural

# Worst first. The pill colour is the page's own: broken, look, muted, working.
STATUSES = {
    "security": ("Security problem", "broken"),
    "eol": ("End of life", "broken"),
    "eol_soon": ("End of life soon", "look"),
    "deprecated": ("Deprecated", "look"),
    "outdated": ("Outdated", "muted"),
    "checking": ("Checking", "muted"),
    "ok": ("OK", "working"),
}
ORDER = list(STATUSES)
PROBLEMS = {"security", "eol", "eol_soon", "deprecated"}
GROUPS = (("runtime", "Language"), ("direct", "Packages your app names"),
          ("indirect", "Packages pulled in by other packages"))
FIXED_IN = re.compile(r"fixed in ([^\s)]+)\)\s*$")


def dated(value: str | None) -> str | None:
    when = as_date(value)
    return f"{day(when)} {when.year}" if when else None


def newer(latest: str | None, current: str) -> bool:
    return bool(latest) and bool(version_key(latest)) and version_key(latest) > version_key(current)


def row(dep: Dependency, found: dict[str, Incident], checked: bool) -> dict:
    """One package: its status (the worst thing known about it) and a
    sentence for each thing known."""
    notes: list[str] = []
    statuses: list[str] = []

    # Security problems: a serious one with a fix is an open "advisory" problem;
    # the lesser ones, and those with no fix yet, are kept on the package.
    serious = found.get("advisory")
    runtime = found.get("runtime")
    lesser = [str(i) for i in (dep.issues or [])]
    serious_count = (serious.data or {}).get("count", 1) if serious else 0
    count = serious_count + len(lesser)
    fixes = [(serious.data or {}).get("to")] if serious else []
    lesser_fixes = [m.group(1) for m in map(FIXED_IN.search, lesser) if m]
    unfixed = len(lesser) - len(lesser_fixes)
    fixes = [f for f in fixes + lesser_fixes if f]
    if runtime is not None:
        missed = (runtime.data or {}).get("missed") or 1
        to = (runtime.data or {}).get("to")
        statuses.append("security")
        notes.append(f"Missing {plural(missed, 'security release')}." + (f" Version {to} has the fixes." if to else ""))
    if count:
        statuses.append("security")
        if not fixes:
            fix = " No fix yet."
        elif not unfixed:
            fix = f" Version {max(fixes, key=version_key)} fixes {'it' if count == 1 else 'all of them'}."
        else:
            fix = f" Version {max(fixes, key=version_key)} fixes some; {unfixed:,} {'has' if unfixed == 1 else 'have'} no fix yet."
        notes.append(f"{plural(count, 'known security problem')}.{fix}")

    # End of life, from the same endoflife.date lookup that raised the problem.
    eol = found.get("eol")
    if eol is not None:
        data = eol.data or {}
        over = data.get("over")
        if over is None:  # raised before the table existed
            over = "no longer gets" in eol.title
        when = dated(data.get("ends"))
        move = f" Move to {data['name']} {data['move_to']}." if data.get("name") and data.get("move_to") else ""
        if over:
            statuses.append("eol")
            notes.append(f"Support ended{f' on {when}' if when else ''}, so new security problems "
                         f"will not be fixed.{move}")
        else:
            statuses.append("eol_soon")
            notes.append(f"Support ends{f' on {when}' if when else ' soon'}.{move}")

    if dep.deprecated:
        statuses.append("deprecated")
        reason = dep.deprecated.strip().rstrip(".")
        reason = reason if len(reason) <= 160 else reason[:157].rstrip() + "…"
        notes.append(f"Its authors deprecated this version: “{reason}”.")

    if newer(dep.latest, dep.version):
        statuses.append("outdated")
    if not statuses:
        statuses.append("ok" if checked else "checking")
        if not checked:
            notes.append("Being looked up. This takes a few minutes after the list is sent.")
    status = min(statuses, key=ORDER.index)
    label, pill = STATUSES[status]
    known = PRODUCTS.get((dep.ecosystem, dep.name.lower()))
    return {
        "ecosystem": dep.ecosystem, "name": dep.name,
        "display": known[1] if known and dep.ecosystem == "runtime" else dep.name,
        "label": "language" if dep.ecosystem == "runtime" else dep.ecosystem,
        "level": "runtime" if dep.ecosystem == "runtime" else "direct" if dep.direct else "indirect",
        "current": dep.version, "latest": dep.latest, "status": status, "status_label": label, "pill": pill,
        "problem": status in PROBLEMS, "notes": notes,
    }


def build(project: Project, deps: list[Dependency], incidents: list[Incident]) -> dict:
    """The table, from the package rows and the project's open problems."""
    by_package: dict[tuple, dict[str, Incident]] = {}
    for incident in incidents:
        if incident.ended_at is not None or incident.kind not in ("advisory", "eol", "runtime"):
            continue
        data = incident.data or {}
        if incident.kind == "runtime":
            key = ("runtime", incident.key)
        elif incident.kind == "eol":
            key = (data.get("ecosystem"), data.get("package"))
        else:
            key = (data.get("ecosystem"), data.get("name"))
        by_package.setdefault(key, {})[incident.kind] = incident

    rows = []
    for dep in deps:
        # deps.dev answers for each package version; the rest (the language,
        # PHP packages) are covered by the project's daily pass.
        checked = (dep.facts_checked_at if dep.ecosystem in SYSTEMS else project.freshness_checked_at) is not None
        rows.append(row(dep, by_package.get((dep.ecosystem, dep.name), {}), checked))

    def order(r):
        return ORDER.index(r["status"]), r["display"].lower()

    groups = []
    hidden = hidden_checking = 0
    for level, title in GROUPS:
        mine = sorted((r for r in rows if r["level"] == level), key=order)
        if level == "indirect":
            shown = [r for r in mine if r["problem"]]
            hidden = len(mine) - len(shown)
            hidden_checking = sum(1 for r in mine if r["status"] == "checking")
            mine = shown
        if mine or (level == "indirect" and hidden):
            groups.append({"level": level, "title": title, "rows": mine})
    sent = project.dependencies_sent_at
    hidden_note = None
    if hidden:
        hidden_note = (f"{plural(hidden, 'more package')} pulled in by other packages "
                       f"{'has' if hidden == 1 else 'have'} no known problem.")
        if hidden_checking:
            hidden_note += (f" {hidden_checking:,} of them {'is' if hidden_checking == 1 else 'are'} "
                            "still being looked up.")
    return {
        "total": len(rows),
        "problems": sum(1 for r in rows if r["problem"]),
        "sent_at": sent,
        "sent": f"{day(sent)} {sent.year}" if sent else None,
        "groups": groups,
        "hidden_indirect": hidden,
        "hidden_checking": hidden_checking,
        "hidden_note": hidden_note,
    }


async def table(db, project: Project) -> dict:
    deps = (await db.scalars(select(Dependency).where(Dependency.project_id == project.id))).all()
    incidents = (await db.scalars(select(Incident).where(
        Incident.project_id == project.id, Incident.ended_at.is_(None),
        Incident.kind.in_(("advisory", "eol", "runtime"))))).all()
    return build(project, list(deps), list(incidents))


def as_json(found: dict) -> dict:
    """For GET /api/dependencies and `npx prodbat status`."""
    return {
        "sent_at": found["sent_at"].isoformat() if found["sent_at"] else None,
        "total": found["total"], "problems": found["problems"],
        "hidden_indirect": found["hidden_indirect"],
        "groups": [{"level": g["level"], "title": g["title"], "rows": [
            {k: r[k] for k in ("ecosystem", "name", "current", "latest", "status", "status_label", "notes")}
            for r in g["rows"]]} for g in found["groups"]],
    }


def as_text(found: dict) -> list[str]:
    """Lines for `npx prodbat status`: the packages with a problem."""
    lines = []
    for group in found["groups"]:
        for r in group["rows"]:
            if not r["problem"]:
                continue
            note = f" {' '.join(r['notes'])}" if r["notes"] else ""
            lines.append(f"- {r['display']} {r['current']} ({r['label']}, {group['level']}): "
                         f"{r['status_label']}.{note}")
    return lines
