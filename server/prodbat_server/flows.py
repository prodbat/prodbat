"""Which requests are part of paying, signing in or signing up.

These matter more than anything else on the site, so one failure is
enough to tell the owner, and the alert says what stopped working
instead of naming a route.
"""

import re

from .models import Project

# Matched against the path, at the start of a path segment or word.
PATTERNS = [
    ("pay", re.compile(r"(^|[/_.\-])(checkout|payments?|pay|billing|subscri\w*|stripe|purchase|orders?|cart|paddle|lemonsqueezy)(?=$|[/_.\-?])", re.I)),
    ("signup", re.compile(r"(^|[/_.\-])(signup|sign-up|sign_up|register|registration|join|onboarding)(?=$|[/_.\-?])", re.I)),
    ("signin", re.compile(r"(^|[/_.\-])(login|log-in|log_in|signin|sign-in|sign_in|auth|sessions?|oauth|sso|magic-link|password)(?=$|[/_.\-?])", re.I)),
]

# flow -> (what people cannot do, what the page is called)
SAY = {
    "pay": ("People cannot pay", "checkout"),
    "signin": ("People cannot sign in", "sign-in"),
    "signup": ("People cannot sign up", "sign-up"),
}


def flow_for(project: Project | None, route: str) -> str | None:
    """`route` is a path, or "POST /api/checkout"."""
    path = route.split(" ", 1)[-1]
    for flow, prefixes in ((project.flows if project else None) or {}).items():
        if flow in SAY and any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes or []):
            return flow
    for flow, pattern in PATTERNS:
        if pattern.search(path):
            return flow
    return None


LINK = re.compile(r"""href=["']([^"'#?]+)""", re.I)


def discover_pages(domain: str, html: str) -> list[str]:
    """Sign-in, sign-up and pricing pages linked from the home page, so
    they can be loaded from outside as well. At most three."""
    found: list[str] = []
    for href in LINK.findall(html):
        href = re.sub(rf"^https?://(www\.)?{re.escape(domain)}", "", href.strip())
        if not href.startswith("/") or href.startswith("//") or len(href) > 100:
            continue
        path = href.rstrip("/") or "/"
        last = path.rsplit("/", 1)[-1]
        if path != "/" and "." not in last and path not in found and (
                flow_for(None, path) in ("signin", "signup") or last.lower() == "pricing"):
            found.append(path)
    return found[:3]
