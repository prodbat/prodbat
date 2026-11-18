"""Name where a visitor came from.

Returns (name, kind). kind is one of: ai, search, social, referral,
campaign, direct, internal. name is None for direct and internal.
"""

import re
from urllib.parse import parse_qs, urlsplit

# (domain, display name, kind). A host matches when it equals the domain
# or ends with "." + domain. Order matters: first match wins.
KNOWN: list[tuple[str, str, str]] = [
    ("chatgpt.com", "ChatGPT", "ai"),
    ("chat.openai.com", "ChatGPT", "ai"),
    ("claude.ai", "Claude", "ai"),
    ("perplexity.ai", "Perplexity", "ai"),
    ("gemini.google.com", "Gemini", "ai"),
    ("copilot.microsoft.com", "Copilot", "ai"),
    ("chat.deepseek.com", "DeepSeek", "ai"),
    ("chat.mistral.ai", "Mistral", "ai"),
    ("grok.com", "Grok", "ai"),
    ("bing.com", "Bing", "search"),
    ("duckduckgo.com", "DuckDuckGo", "search"),
    ("search.brave.com", "Brave Search", "search"),
    ("search.yahoo.com", "Yahoo", "search"),
    ("yandex.ru", "Yandex", "search"),
    ("baidu.com", "Baidu", "search"),
    ("ecosia.org", "Ecosia", "search"),
    ("kagi.com", "Kagi", "search"),
    ("news.ycombinator.com", "Hacker News", "social"),
    ("t.co", "X", "social"),
    ("x.com", "X", "social"),
    ("twitter.com", "X", "social"),
    ("reddit.com", "Reddit", "social"),
    ("facebook.com", "Facebook", "social"),
    ("instagram.com", "Instagram", "social"),
    ("linkedin.com", "LinkedIn", "social"),
    ("lnkd.in", "LinkedIn", "social"),
    ("youtube.com", "YouTube", "social"),
    ("producthunt.com", "Product Hunt", "social"),
    ("bsky.app", "Bluesky", "social"),
    ("threads.net", "Threads", "social"),
    ("github.com", "GitHub", "social"),
]

# google.com, www.google.co.uk, google.de ... (gemini.google.com is caught above)
_GOOGLE = re.compile(r"(^|\.)google\.[a-z]{2,3}(\.[a-z]{2})?$")

# What people type into utm_source / ref when they do not use a hostname.
_ALIASES = {name.lower().replace(" ", ""): (name, kind) for _, name, kind in KNOWN}
_ALIASES.update(
    {
        "google": ("Google", "search"),
        "twitter": ("X", "social"),
        "hn": ("Hacker News", "social"),
        "openai": ("ChatGPT", "ai"),
    }
)

_TAG_PARAMS = ("utm_source", "ref", "source")


def _match_host(host: str) -> tuple[str, str] | None:
    host = host.lower().removeprefix("www.")
    for domain, name, kind in KNOWN:
        if host == domain or host.endswith("." + domain):
            return name, kind
    if _GOOGLE.search(host):
        return "Google", "search"
    return None


def classify(referrer: str | None, page_url: str, own_host: str) -> tuple[str | None, str]:
    # A tag on the landing URL is the site owner's own label, so it wins.
    query = parse_qs(urlsplit(page_url).query)
    for param in _TAG_PARAMS:
        value = (query.get(param) or [""])[0].strip()
        if value:
            known = _match_host(value) or _ALIASES.get(value.lower().replace(" ", ""))
            return known or (value[:100], "campaign")

    host = (urlsplit(referrer).hostname or "") if referrer else ""
    host = host.lower().removeprefix("www.")
    if not host:
        return None, "direct"
    if host == own_host:
        return None, "internal"
    return _match_host(host) or (host[:100], "referral")
