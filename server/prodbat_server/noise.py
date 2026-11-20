"""Telling the site's own errors from everyone else's.

A browser error reaches Prodbat from any script running in the page, not
only the site's. Browser extensions, and the built-in browsers of apps like
Instagram, add scripts of their own, and their errors look the same. An
error counts as the site's when its trace runs through a JavaScript file the
site serves (its own domain or a subdomain). Errors from extensions, from
other sites' scripts, or from a script inside the page that isn't in any of
the site's files, are kept and shown, but are not alerts.

An error with no location at all could be anyone's, so it counts as the
site's, except "Script error.", which browsers send in place of an error from
another site's script.

Also: a short browser name from the User-Agent ("Safari 18", "Instagram's
built-in browser"), so an agent can see which browsers an error happens in.
Only the name and major version are kept, never the whole string.
"""

import re
from urllib.parse import urlsplit

SITE = "site"
ELSEWHERE = "elsewhere"
EXTENSION_SCHEMES = {"chrome-extension", "moz-extension", "safari-extension", "safari-web-extension",
                     "webkit-masked-url", "ms-browser-extension", "edge-extension"}
URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://[^\s()<>\"']+", re.I)
POSITION = re.compile(r"(:\d+){1,2}$")
SCRIPT_FILE = re.compile(r"\.(m|c)?jsx?$", re.I)

# In-app browsers first: they also say Safari or Chrome.
APPS = [("Instagram", "Instagram"), ("FBAN|FBAV|FB_IAB", "Facebook"), ("musical_ly|BytedanceWebview|TikTok", "TikTok"),
        ("LinkedInApp", "LinkedIn"), ("Snapchat", "Snapchat"), ("Pinterest", "Pinterest"), (r"\bLine/", "LINE"),
        ("GSA/", "the Google app"), ("Twitter", "X")]
BROWSERS = [(r"Edg(?:e|A|iOS)?/(\d+)", "Edge"), (r"OPR/(\d+)", "Opera"), (r"SamsungBrowser/(\d+)", "Samsung Internet"),
            (r"Firefox/(\d+)|FxiOS/(\d+)", "Firefox"), (r"CriOS/(\d+)", "Chrome"), (r"Chrome/(\d+)", "Chrome"),
            (r"Version/(\d+)[.\d]* (?:Mobile/\S+ )?Safari/", "Safari")]


def browser_name(user_agent: str) -> str:
    for pattern, app in APPS:
        if re.search(pattern, user_agent):
            return f"{app}'s built-in browser"
    for pattern, name in BROWSERS:
        m = re.search(pattern, user_agent)
        if m:
            major = next((g for g in m.groups() if g), None)
            return f"{name} {major}" if major else name
    if "AppleWebKit" in user_agent and ("iPhone" in user_agent or "iPad" in user_agent):
        return "an iOS app's built-in browser"
    return "another browser"


def locations(file: str, stack: str) -> list[str]:
    found = [file] if file else []
    found += [m.group(0) for m in URL.finditer(stack or "")]
    return list(dict.fromkeys(POSITION.sub("", loc.rstrip(".,;")) for loc in found))


def ours(host: str, domain: str) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def classify(message: str, file: str, stack: str, domain: str) -> tuple[str, str]:
    """(SITE or ELSEWHERE, why) for one browser error on `domain`."""
    places = locations(file, stack)
    schemes = {urlsplit(p).scheme.lower() for p in places}
    if schemes & EXTENSION_SCHEMES:
        return ELSEWHERE, "It comes from a browser extension."
    for place in places:
        url = urlsplit(place)
        if url.scheme in ("http", "https") and ours(url.hostname, domain) and SCRIPT_FILE.search(url.path):
            return SITE, ""
    if not places:
        if message.strip().lower().startswith("script error"):
            return ELSEWHERE, ("It comes from another site's script: browsers hide the details of those errors and "
                               "say only “Script error.”")
        return SITE, ""
    others = sorted({urlsplit(p).hostname for p in places if urlsplit(p).hostname and not ours(urlsplit(p).hostname, domain)})
    if others:
        return ELSEWHERE, f"It comes from a script served by {', '.join(others[:3])}, not by the site."
    return ELSEWHERE, ("It comes from a script inside the page that isn't in any of the site's files. That is usually "
                       "one a browser extension or an app's built-in browser added.")
