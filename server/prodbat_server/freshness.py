"""Is the language, the framework and every package still looked after?

Three public sources, each the central one for its question:

- https://endoflife.date: when a language or framework release line
  stops getting security fixes.
- https://deps.dev (Google's index of npm, PyPI, RubyGems, Go and
  Cargo): whether the authors have deprecated the exact version in use,
  and what the newest version is.
- https://nodejs.org/dist/index.json: which Node.js releases were
  security releases.

Unsupported or deprecated needs a look. Merely being behind is never an
alert; it is listed on the page and in the weekly email.
"""

import datetime as dt
import re
from urllib.parse import quote

import httpx
from sqlalchemy import select

from .config import Settings
from .facts import DEPRECATED_TTL, EOL_TTL, LATEST_TTL, NODE_RELEASES_TTL, eol_facts, package_facts, stale
from .facts import key as fact_key
from .incidents import digest, report, resolve
from .models import Dependency, Incident, PackageFacts, Project
from .words import day, plural

EOL_API = "https://endoflife.date/api/{product}.json"
DEPS_DEV = "https://api.deps.dev"
NODE_RELEASES = "https://nodejs.org/dist/index.json"
WARN = dt.timedelta(days=60)
MAX_LATEST_LOOKUPS = 150

# Our ecosystem names -> deps.dev's.
SYSTEMS = {"npm": "NPM", "PyPI": "PYPI", "RubyGems": "RUBYGEMS", "Go": "GO", "crates.io": "CARGO"}

# (ecosystem, package name) -> (endoflife.date product, display name).
# Only packages that ARE the product: the npm package "redis" is a client
# with its own version numbers, not the Redis server, so it is not here.
PRODUCTS = {
    ("runtime", "node"): ("nodejs", "Node.js"),
    ("runtime", "python"): ("python", "Python"),
    ("runtime", "ruby"): ("ruby", "Ruby"),
    ("runtime", "php"): ("php", "PHP"),
    ("runtime", "go"): ("go", "Go"),
    ("runtime", "bun"): ("bun", "Bun"),
    ("runtime", "deno"): ("deno", "Deno"),
    ("npm", "next"): ("nextjs", "Next.js"),
    ("npm", "react"): ("react", "React"),
    ("npm", "react-native"): ("react-native", "React Native"),
    ("npm", "vue"): ("vue", "Vue"),
    ("npm", "nuxt"): ("nuxt", "Nuxt"),
    ("npm", "@angular/core"): ("angular", "Angular"),
    ("npm", "angular"): ("angularjs", "AngularJS"),
    ("npm", "express"): ("express", "Express"),
    ("npm", "svelte"): ("svelte", "Svelte"),
    ("npm", "@nestjs/core"): ("nestjs", "NestJS"),
    ("npm", "ember-source"): ("emberjs", "Ember"),
    ("npm", "electron"): ("electron", "Electron"),
    ("npm", "@ionic/core"): ("ionic", "Ionic"),
    ("npm", "jquery"): ("jquery", "jQuery"),
    ("npm", "bootstrap"): ("bootstrap", "Bootstrap"),
    ("npm", "tailwindcss"): ("tailwind-css", "Tailwind CSS"),
    ("npm", "vuetify"): ("vuetify", "Vuetify"),
    ("npm", "eslint"): ("eslint", "ESLint"),
    ("npm", "@strapi/strapi"): ("strapi", "Strapi"),
    ("PyPI", "django"): ("django", "Django"),
    ("PyPI", "numpy"): ("numpy", "NumPy"),
    ("PyPI", "wagtail"): ("wagtail", "Wagtail"),
    ("PyPI", "ansible"): ("ansible", "Ansible"),
    ("PyPI", "ansible-core"): ("ansible-core", "Ansible Core"),
    ("RubyGems", "rails"): ("rails", "Rails"),
    ("Packagist", "laravel/framework"): ("laravel", "Laravel"),
    ("Packagist", "symfony/symfony"): ("symfony", "Symfony"),
    ("Packagist", "drupal/core"): ("drupal", "Drupal"),
}

def parts(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def as_date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def find_cycle(cycles: list[dict], version: str) -> dict | None:
    """The release line a version belongs to: 3.12.1 -> "3.12", 22.4.0 -> "22"."""
    v = parts(version)
    for size in (2, 1):
        want = ".".join(str(n) for n in v[:size])
        for cycle in cycles:
            if str(cycle.get("cycle")) == want:
                return cycle
    return None


def is_over(cycle: dict, today: dt.date) -> bool:
    eol = cycle.get("eol")
    return eol is True or (as_date(eol) is not None and as_date(eol) <= today)


def supported_cycle(cycles: list[dict], today: dt.date) -> str | None:
    """The newest line still supported, preferring a long-term-support one."""
    alive = [c for c in cycles if not is_over(c, today)]
    lts = [c for c in alive if c.get("lts") is True or (as_date(c.get("lts")) and as_date(c["lts"]) <= today)]
    best = max(lts or alive, key=lambda c: parts(str(c["cycle"])), default=None)
    return str(best["cycle"]) if best else None


def majors_behind(dep: Dependency) -> int:
    if not dep.latest or dep.ecosystem == "runtime":
        return 0
    have, newest = parts(dep.version), parts(dep.latest)
    return max(0, newest[0] - have[0]) if have and newest else 0


def patch_behind(dep: Dependency) -> bool:
    """A language that is not on the newest release of its own line."""
    return (dep.ecosystem == "runtime" and bool(dep.latest) and len(parts(dep.version)) == 3
            and parts(dep.latest) > parts(dep.version))


def deps_dev_version(dep) -> str:
    return f"v{dep.version}" if dep.ecosystem == "Go" and not dep.version.startswith("v") else dep.version


async def check_deprecations(client: httpx.AsyncClient, packages: list[PackageFacts], now: dt.datetime) -> None:
    """Ask deps.dev about every package version not asked about lately.
    One request covers up to a thousand of them."""
    due = [p for p in packages if p.ecosystem in SYSTEMS and stale(p.deprecated_checked_at, DEPRECATED_TTL, now)]
    for start in range(0, len(due), 1000):
        chunk = due[start:start + 1000]
        body = {"requests": [{"versionKey": {"system": SYSTEMS[d.ecosystem], "name": d.name,
                                             "version": deps_dev_version(d)}} for d in chunk]}
        found: dict[tuple, dict] = {}
        while True:
            response = await client.post(f"{DEPS_DEV}/v3alpha/versionbatch", json=body)
            response.raise_for_status()
            data = response.json()
            for item in data.get("responses", []):
                key = (item.get("request") or {}).get("versionKey") or {}
                if item.get("version"):
                    found[(key.get("system"), key.get("name"), key.get("version"))] = item["version"]
            if not data.get("nextPageToken"):
                break
            body["pageToken"] = data["nextPageToken"]
        for package in chunk:
            info = found.get((SYSTEMS[package.ecosystem], package.name, deps_dev_version(package)))
            package.deprecated_checked_at = now
            if info is not None:
                reason = (info.get("deprecatedReason") or "").strip()
                package.deprecated = (reason or "deprecated by its authors")[:500] if info.get("isDeprecated") else None


async def newest_version(client: httpx.AsyncClient, dep) -> str | None:
    try:
        response = await client.get(
            f"{DEPS_DEV}/v3/systems/{SYSTEMS[dep.ecosystem].lower()}/packages/{quote(dep.name, safe='')}")
        if response.status_code != 200:
            return None
        default = next((v for v in response.json().get("versions", []) if v.get("isDefault")), None)
        return default["versionKey"]["version"].removeprefix("v") if default else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def check_freshness(db, project: Project, client: httpx.AsyncClient, settings: Settings,
                          now: dt.datetime) -> None:
    """One site's language, frameworks and packages. Run by the queue once
    a day per site, and straight after its package list changes. Only
    facts that are missing or past their time (facts.py) are asked for.
    The caller commits."""
    project.freshness_checked_at = now
    deps = (await db.scalars(select(Dependency).where(Dependency.project_id == project.id))).all()
    kept = await package_facts(db, [d for d in deps if d.ecosystem in SYSTEMS])
    today = now.date()
    wrong: set[tuple[str, str]] = set()
    new: list = []

    async def say(kind, key, **fields):
        wrong.add((kind, key))
        await report(db, project, settings, kind=kind, key=key, level="look", who="Nobody yet.", started_at=now,
                     gather=new, **fields)

    # End of life, for the language and for frameworks endoflife.date knows.
    for dep in deps:
        known = PRODUCTS.get((dep.ecosystem, dep.name.lower()))
        if not known:
            continue
        product, name = known
        cycles = await eol_facts(db, client, product, EOL_API.format(product=product), EOL_TTL, now)
        if not cycles:
            continue
        cycle = find_cycle(cycles, dep.version)
        if cycle is None:
            continue
        if dep.ecosystem == "runtime":
            dep.latest = cycle.get("latest") or dep.latest
        line = f"{name} {cycle['cycle']}"
        ends = as_date(cycle.get("eol"))
        move_to = supported_cycle(cycles, today)
        todo = (f"Ask your coding agent to move to {name} {move_to}, run the tests, and deploy."
                if move_to else "Ask your coding agent which version to move to.")
        # What the dependencies table on the page shows for this row, too.
        facts = {"name": name, "version": dep.version, "line": str(cycle["cycle"]), "move_to": move_to,
                 "ecosystem": dep.ecosystem, "package": dep.name, "ends": ends.isoformat() if ends else None,
                 "over": is_over(cycle, today)}
        if is_over(cycle, today):
            when = f" on {day(ends)} {ends.year}" if ends else ""
            await say("eol", product, title=f"{line} no longer gets security fixes", todo=todo, data=facts,
                      what=f"Your app runs {name} {dep.version}. Support for {line} ended{when}, so new "
                           f"security problems in it will not be fixed.")
        elif ends and ends - today <= WARN:
            await say("eol", product, todo=todo, data=facts,
                      title=f"{line} stops getting security fixes in {plural((ends - today).days, 'day')}",
                      what=f"Your app runs {name} {dep.version}. Support for {line} ends on {day(ends)} {ends.year}.")

    # Node.js says which of its releases fixed security problems.
    node = next((d for d in deps if d.ecosystem == "runtime" and d.name == "node" and len(parts(d.version)) == 3), None)
    if node is not None and ("eol", "nodejs") not in wrong:
        releases = await eol_facts(db, client, "nodejs-releases", NODE_RELEASES, NODE_RELEASES_TTL, now) or []
        newer = [r for r in releases if parts(r["version"])[:1] == parts(node.version)[:1]
                 and parts(r["version"]) > parts(node.version)]
        missed = sum(1 for r in newer if r.get("security"))
        if newer:
            node.latest = max(newer, key=lambda r: parts(r["version"]))["version"].removeprefix("v")
        if missed:
            await say("runtime", "node", data={"version": node.version, "to": node.latest, "missed": missed},
                      title=f"Node.js {node.version} is missing {plural(missed, 'security release')}",
                      what=f"Your app runs Node.js {node.version}. {plural(missed, 'release')} since then fixed "
                           f"security problems; the newest Node.js {parts(node.version)[0]} is {node.latest}.",
                      todo=f"Ask your coding agent to move to Node.js {node.latest}. It is the same major "
                           f"version, so it should be a safe upgrade. Run the tests and deploy.")

    # Deprecated or withdrawn by their authors: every package, named or pulled in.
    try:
        await check_deprecations(client, list(kept.values()), now)
    except (httpx.HTTPError, ValueError, KeyError):
        pass  # what was known before still holds
    for dep in deps:
        package = kept.get(fact_key(dep))
        if package is not None and package.deprecated_checked_at is not None:
            dep.deprecated, dep.facts_checked_at = package.deprecated, package.deprecated_checked_at
    advised = {i.key for i in await db.scalars(select(Incident).where(
        Incident.project_id == project.id, Incident.kind == "advisory", Incident.ended_at.is_(None)))}
    for dep in deps:
        key = f"{dep.ecosystem}/{dep.name}"
        # Only the ones the project names itself are worth an email, and not
        # when a security alert for the same package is already open.
        if dep.deprecated and dep.direct and key not in advised:
            await say("deprecated", key, title=f"{dep.name} is deprecated",
                      data={"ecosystem": dep.ecosystem, "name": dep.name, "version": dep.version},
                      what=f"The authors of {dep.name} say of version {dep.version}: “{dep.deprecated.rstrip('.')}”.",
                      todo=f"Ask your coding agent what replaces {dep.name}, or which version to move to.")

    digest(db, project, new, settings, "the versions your app runs on")
    for incident in await db.scalars(select(Incident).where(
            Incident.project_id == project.id, Incident.kind.in_(("eol", "runtime", "deprecated")),
            Incident.ended_at.is_(None))):
        if (incident.kind, incident.key) not in wrong:
            resolve(db, project, incident, settings, now)

    # The newest version of each package the project names itself.
    for dep in [d for d in deps if d.direct and d.ecosystem in SYSTEMS][:MAX_LATEST_LOOKUPS]:
        package = kept[fact_key(dep)]
        if stale(package.latest_checked_at, LATEST_TTL, now):
            newest = await newest_version(client, package)
            if newest:
                package.latest, package.latest_checked_at = newest, now
        dep.latest = package.latest or dep.latest
