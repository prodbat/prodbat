import pytest

from prodbat_server.sources import classify

OWN = "yourapp.com"
PAGE = "https://yourapp.com/"


@pytest.mark.parametrize(
    "referrer, page, expected",
    [
        (None, PAGE, (None, "direct")),
        ("", PAGE, (None, "direct")),
        ("https://www.yourapp.com/pricing", PAGE, (None, "internal")),
        ("https://chatgpt.com/", PAGE, ("ChatGPT", "ai")),
        ("https://claude.ai/chat/abc", PAGE, ("Claude", "ai")),
        ("https://www.perplexity.ai/search", PAGE, ("Perplexity", "ai")),
        ("https://gemini.google.com/app", PAGE, ("Gemini", "ai")),
        ("https://www.google.com/", PAGE, ("Google", "search")),
        ("https://www.google.co.uk/", PAGE, ("Google", "search")),
        ("https://duckduckgo.com/", PAGE, ("DuckDuckGo", "search")),
        ("https://news.ycombinator.com/item?id=1", PAGE, ("Hacker News", "social")),
        ("https://old.reddit.com/r/SaaS", PAGE, ("Reddit", "social")),
        ("https://t.co/abc", PAGE, ("X", "social")),
        ("https://www.someblog.org/post", PAGE, ("someblog.org", "referral")),
        ("https://notgoogle.com/", PAGE, ("notgoogle.com", "referral")),
        # A tag on the landing URL wins over the referrer.
        (None, PAGE + "?utm_source=chatgpt.com", ("ChatGPT", "ai")),
        ("https://www.google.com/", PAGE + "?utm_source=newsletter", ("newsletter", "campaign")),
        (None, PAGE + "?ref=producthunt", ("Product Hunt", "social")),
        (None, PAGE + "?ref=hn", ("Hacker News", "social")),
    ],
)
def test_classify(referrer, page, expected):
    assert classify(referrer, page, OWN) == expected
