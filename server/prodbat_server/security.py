"""A look for obvious holes, from outside, for the free check.

Everything here is a plain GET an ordinary browser could make: the home
page (already loaded by the check), http:// to see where it goes, and
three well-known files that should never be public. No forms, no payloads,
no lists of paths to guess. What a file holds is never kept, logged or
returned; only whether it is there.
"""

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .advisories import known_problems, version_key
from .words import plural

TIMEOUT = 5.0
READ_AT_MOST = 4096
OSV_TIMEOUT = 5.0


@dataclass
class Finding:
    level: str  # broken, look or minor
    kind: str
    plain: str  # for the owner, no jargon
    engineer: str  # for whoever fixes it
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"level": self.level, "kind": self.kind, "plain": self.plain, "engineer": self.engineer, **self.data}


def same_site(host: str | None, domain: str) -> bool:
    bare = domain.removeprefix("www.")
    return (host or "").lower() in {bare, "www." + bare}


# ---- http:// to https:// ------------------------------------------------

async def https_redirect(http: httpx.AsyncClient, domain: str) -> bool | None:
    """True if http:// ends up on https:// of the same site, False if it
    answers over plain http, None if it does not answer at all (or goes
    to another site, which says nothing about this one)."""
    url = f"http://{domain}/"
    for _ in range(4):
        try:
            response = await http.get(url, follow_redirects=False, timeout=TIMEOUT)
        except httpx.HTTPError:
            return None
        if not response.is_redirect:
            return False if response.is_success else None
        target = urlsplit(str(response.url.join(response.headers.get("location", ""))))
        if not same_site(target.hostname, domain):
            return None
        if target.scheme == "https":
            return True
        url = target.geturl()
    return None


# ---- files that should never be public ----------------------------------

ENV_LINE = re.compile(r"^(export\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*=")
GIT_HEAD = re.compile(r"^(ref: refs/\S+|[0-9a-f]{40})$")


def looks_like_env(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines or any(line.startswith("<") for line in lines):
        return False
    return sum(bool(ENV_LINE.match(line)) for line in lines) >= max(1, 0.8 * len(lines))


def looks_like_git_config(text: str) -> bool:
    return "[core]" in text and "<html" not in text.lower()


def looks_like_git_head(text: str) -> bool:
    return bool(GIT_HEAD.match(text.strip()))


FILES = [("/.env", looks_like_env), ("/.git/config", looks_like_git_config), ("/.git/HEAD", looks_like_git_head)]


async def exposed(http: httpx.AsyncClient, domain: str, path: str, looks_right) -> bool | None:
    """True only for a 200 whose first few KB read like the real file, so a
    site that answers every address with its home page is not flagged.
    None if we could not tell."""
    try:
        async with http.stream("GET", f"https://{domain}{path}", follow_redirects=False,
                               timeout=TIMEOUT) as response:
            if response.status_code != 200:
                return False
            if "html" in response.headers.get("content-type", "").lower():
                return False
            start = b""
            async for chunk in response.aiter_bytes():
                start += chunk
                if len(start) >= READ_AT_MOST:
                    break
    except httpx.HTTPError:
        return None
    return looks_right(start[:READ_AT_MOST].decode("utf-8", "ignore"))


async def passive_requests(http: httpx.AsyncClient, domain: str) -> dict:
    """The requests that do not need the home page first, all at once."""
    redirect, *files = await asyncio.gather(
        https_redirect(http, domain), *(exposed(http, domain, path, test) for path, test in FILES))
    return {"https_redirect": redirect, "files": dict(zip((path for path, _ in FILES), files))}


# ---- headers and cookies on the home page -------------------------------

def missing_headers(headers: dict) -> list[str]:
    headers = {k.lower(): v for k, v in headers.items()}
    csp = headers.get("content-security-policy", "")
    missing = []
    if "strict-transport-security" not in headers:
        missing.append("Strict-Transport-Security")
    if not csp:
        missing.append("Content-Security-Policy")
    if "x-frame-options" not in headers and "frame-ancestors" not in csp.lower():
        missing.append("X-Frame-Options (or frame-ancestors in Content-Security-Policy)")
    if headers.get("x-content-type-options", "").strip().lower() != "nosniff":
        missing.append("X-Content-Type-Options: nosniff")
    return missing


SESSION_LIKE = re.compile(r"sess|sid|auth|token|jwt|login|remember", re.I)


def weak_cookies(set_cookies: list[str]) -> list[dict]:
    """Names only; a cookie's value is never kept. HttpOnly is asked of
    cookies that look like sign-in sessions, since analytics and preference
    cookies are often meant to be read by the page's own scripts."""
    weak = []
    for raw in set_cookies:
        name, _, rest = raw.partition("=")
        flags = {part.strip().split("=", 1)[0].lower() for part in rest.split(";")[1:]}
        missing = []
        if "secure" not in flags:
            missing.append("Secure")
        if "httponly" not in flags and SESSION_LIKE.search(name):
            missing.append("HttpOnly")
        if missing:
            weak.append({"name": name.strip()[:60], "missing": missing})
    return weak


# ---- versions the site tells everyone -----------------------------------

# (pattern, display name, OSV ecosystem, OSV package). No package: shown,
# but there is nowhere public to match it against.
SOFTWARE = [
    (r"next\.?js", "Next.js", "npm", "next"),
    (r"nuxt", "Nuxt", "npm", "nuxt"),
    (r"express", "Express", "npm", "express"),
    (r"astro", "Astro", "npm", "astro"),
    (r"gatsby", "Gatsby", "npm", "gatsby"),
    (r"docusaurus", "Docusaurus", "npm", "@docusaurus/core"),
    (r"ghost", "Ghost", "npm", "ghost"),
    (r"drupal", "Drupal", "Packagist", "drupal/core"),
    (r"wordpress", "WordPress", None, None),
    (r"php", "PHP", None, None),
]
VERSION = r"[\s/]+v?(\d+\.\d+(?:\.\d+)?)\b"
GENERATOR = re.compile(r"<meta\s[^>]*name=[\"']generator[\"'][^>]*>", re.I)
CONTENT = re.compile(r"content=[\"']([^\"']{1,200})[\"']", re.I)


def leaked_versions(headers: dict, body: str) -> list[dict]:
    headers = {k.lower(): v for k, v in headers.items()}
    places = [("x-powered-by header", headers.get("x-powered-by", "")), ("server header", headers.get("server", ""))]
    for tag in GENERATOR.findall(body[:200_000])[:3]:
        content = CONTENT.search(tag)
        if content:
            places.append(("generator meta tag", content[1]))
    found, seen = [], set()
    for where, value in places:
        for pattern, name, ecosystem, package in SOFTWARE:
            match = re.search(r"\b" + pattern + VERSION, value, re.I)
            if match and name not in seen:
                seen.add(name)
                found.append({"name": name, "version": match[1], "where": where,
                              "ecosystem": ecosystem, "package": package})
    return found


async def match_advisories(http: httpx.AsyncClient, software: list[dict]) -> None:
    """Adds `advisories` to each piece of software OSV knows. A failed or
    slow lookup leaves it out rather than holding up the check."""
    known = [s for s in software if s["package"]]

    async def one(s):
        try:
            s["advisories"] = await asyncio.wait_for(
                known_problems(http, s["ecosystem"], s["package"], s["version"], OSV_TIMEOUT), OSV_TIMEOUT + 1)
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError):
            pass

    await asyncio.gather(*(one(s) for s in known))


# ---- what to say --------------------------------------------------------

RANK = {"critical": 0, "high": 1, "moderate": 2, "low": 3}

def findings(domain: str, home_ok: bool, home_headers: dict, home_cookies: list[str], home_same_site: bool,
             passive: dict, software: list[dict]) -> list[Finding]:
    found: list[Finding] = []
    files = passive["files"]

    if files.get("/.env"):
        found.append(Finding(
            "broken", "exposed_file",
            f"Anyone can download your .env file at {domain}/.env. It usually holds passwords and keys, "
            "so treat every one in it as leaked.",
            f"GET https://{domain}/.env answers 200 with KEY=VALUE lines. Deny dotfiles at the host or web server, "
            "take the file out of the deployed directory, then rotate every secret it held.",
            {"path": "/.env", "exposed": True}))
    git = [path for path in ("/.git/config", "/.git/HEAD") if files.get(path)]
    if git:
        found.append(Finding(
            "broken", "exposed_file",
            f"Your code's history can be downloaded from {domain}/.git, including any password that was "
            "ever saved in it.",
            f"GET https://{domain}{git[0]} answers 200 with the real file, so the whole .git directory is "
            "likely served. Leave .git out of the deploy, deny /.git at the web server, and rotate any secret "
            "that was ever committed.",
            {"path": git[0], "paths": git, "exposed": True}))

    for s in software:
        problems = s.get("advisories") or []
        if not problems:
            continue
        fixes = [p["fixed"] for p in problems if p["fixed"]]
        fix = max(fixes, key=version_key) if fixes else None
        serious = sum(p["severity"] in ("critical", "high") for p in problems)
        rated = f" ({serious} rated high or critical)" if serious else ""
        if fix and len(fixes) == len(problems):
            upgrade = f" Upgrading to {fix} or later fixes all of them."
        elif fix:
            upgrade = f" Upgrading to {fix} or later fixes {len(fixes)} of them."
        else:
            upgrade = ""
        first = min(problems, key=lambda p: RANK.get(p["severity"], 9))
        example = f"{first['id']} ({first['severity'] or 'unrated'}, " + \
                  (f"fixed in {first['fixed']})" if first["fixed"] else "no fix yet)")
        found.append(Finding(
            "look", "known_vulnerable_version",
            f"Your site says it runs {s['name']} {s['version']}, which has "
            f"{plural(len(problems), 'known security problem')}{rated}.{upgrade}",
            f"The {s['where']} shows {s['name']} {s['version']} ({s['ecosystem']} package {s['package']}). "
            f"OSV.dev lists {len(problems)} {'advisory' if len(problems) == 1 else 'advisories'} for it, "
            f"including {example}. "
            + (f"Upgrade {s['package']} to {fix} or later, " if fix else "Check for a patched release, ")
            + f"and stop sending the version in the {s['where']}.",
            {"name": s["name"], "version": s["version"], "package": s["package"], "ecosystem": s["ecosystem"],
             "fixed": fix, "advisories": [{"id": p["id"], "severity": p["severity"], "fixed": p["fixed"]}
                                          for p in problems[:25]]}))

    if passive["https_redirect"] is False:
        found.append(Finding(
            "look", "no_https_redirect",
            f"People who type http://{domain} get your site without a secure connection, instead of being "
            "moved to the secure address.",
            f"http://{domain}/ answers 200 without redirecting. Add a permanent (301 or 308) redirect from "
            "http:// to https:// at the host or proxy.",
            {}))

    if home_ok and home_same_site:
        missing = missing_headers(home_headers)
        if missing:
            found.append(Finding(
                "minor", "missing_headers",
                "Your home page leaves out some standard settings that tell browsers to protect your visitors. "
                "Most hosts and frameworks can switch them on in one place.",
                f"Response headers missing on https://{domain}/: {', '.join(missing)}.",
                {"missing": missing}))
        weak = weak_cookies(home_cookies)
        if weak:
            names = "; ".join(f"{c['name']} lacks {' and '.join(c['missing'])}" for c in weak[:10])
            found.append(Finding(
                "minor", "cookie_flags",
                "Some cookies your home page sets could travel over an insecure connection or be read by "
                "scripts on the page.",
                f"Set-Cookie on https://{domain}/: {names}. Add Secure to every cookie, and HttpOnly to any "
                "cookie the page's scripts do not need to read.",
                {"cookies": weak[:10]}))
    return found
