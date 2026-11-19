"""The free check's look for obvious holes: passive requests only, and no
false alarms from sites that answer every address with their home page."""

import datetime as dt
import json

import pytest

from prodbat_server import api

INDEX = '<!doctype html><html><head><title>App</title></head><body><div id="root"></div></body></html>'
SAFE = {"strict-transport-security": "max-age=63072000", "content-security-policy": "frame-ancestors 'none'",
        "x-content-type-options": "nosniff", "content-type": "text/html"}
SECRET = "sk_" + "live_" + "51NotARealKeyButLooksLikeOne"  # split so secret scanners don't flag a made-up key
ENV = f"# production\nDATABASE_URL=postgres://app:hunter2@db.internal/app\nSTRIPE_SECRET_KEY={SECRET}\n\nDEBUG=false\n"


@pytest.fixture(autouse=True)
def certificate(monkeypatch):
    monkeypatch.setattr(api, "certificate_expiry", lambda domain: dt.datetime(2031, 2, 14, tzinfo=dt.UTC))


def check(client, url="anysite.com"):
    response = client.get("/api/check", params={"url": url})
    assert response.status_code == 200
    return response.json()


def kinds(result):
    return [(f["level"], f["kind"]) for f in result["security"]["findings"]]


def a_tidy_site(site):
    site.body = INDEX
    site.headers = dict(SAFE)
    site.pages["http://anysite.com/"] = (301, "", {"location": "https://anysite.com/"})


def test_a_tidy_site_has_nothing_to_fix_and_says_what_was_checked(client, site):
    a_tidy_site(site)
    result = check(client)
    assert result["security"]["findings"] == []
    assert result["worth_fixing"] == []
    assert "Your .env file and code history (.git) are not open to the public." in result["working"]
    assert "http://anysite.com moves visitors to the secure address." in result["working"]
    assert result["security"]["https_redirect"] is True
    assert result["security"]["exposed_files"] == {"/.env": False, "/.git/config": False, "/.git/HEAD": False}
    # Only ordinary GETs of the home page, http:// and the three files (plus the usual www and RDAP).
    assert sorted(a for a in site.asked if "anysite.com" in a and "www." not in a) == [
        "http://anysite.com/", "https://anysite.com/", "https://anysite.com/.env",
        "https://anysite.com/.git/HEAD", "https://anysite.com/.git/config"]


def test_a_site_that_serves_its_home_page_for_every_address_is_not_called_exposed(client, site):
    a_tidy_site(site)
    # A single-page app's catch-all: 200 and index.html for /.env and /.git/*,
    # sometimes without saying it is HTML.
    site.pages["/.env"] = (200, INDEX, {"content-type": "text/html; charset=utf-8"})
    site.pages["/.git/config"] = (200, INDEX, {"content-type": "text/plain"})
    site.pages["/.git/HEAD"] = (200, INDEX, {})
    result = check(client)
    assert "exposed_file" not in [kind for _, kind in kinds(result)]
    assert result["security"]["exposed_files"] == {"/.env": False, "/.git/config": False, "/.git/HEAD": False}


def test_a_real_env_file_is_broken_and_its_contents_never_appear(client, site):
    a_tidy_site(site)
    site.pages["/.env"] = (200, ENV, {"content-type": "application/octet-stream"})
    result = check(client)
    assert kinds(result) == [("broken", "exposed_file")]
    finding = result["security"]["findings"][0]
    assert finding["path"] == "/.env" and finding["exposed"] is True
    assert result["worth_fixing"][0].startswith("Anyone can download your .env file at anysite.com/.env.")
    assert "rotate every secret" in finding["engineer"]
    everything = json.dumps(result)
    for secret in (SECRET, "hunter2", "DATABASE_URL", "STRIPE_SECRET_KEY", "postgres://"):
        assert secret not in everything
    assert "not open to the public" not in " ".join(result["working"])


def test_an_exposed_git_directory_is_broken(client, site):
    a_tidy_site(site)
    site.pages["/.git/config"] = (200, '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n', {})
    site.pages["/.git/HEAD"] = (200, "ref: refs/heads/main\n", {})
    result = check(client)
    assert kinds(result) == [("broken", "exposed_file")]
    assert result["security"]["findings"][0]["paths"] == ["/.git/config", "/.git/HEAD"]
    assert "repositoryformatversion" not in json.dumps(result)


def test_a_redirected_or_missing_file_is_not_exposed(client, site):
    a_tidy_site(site)
    site.pages["/.env"] = (302, "", {"location": "https://anysite.com/"})
    site.pages["/.git/config"] = (404, "[core]", {})
    result = check(client)
    assert result["security"]["findings"] == []


def test_missing_headers_and_no_https_redirect_are_reported(client, site):
    site.body = INDEX
    site.headers = {"content-type": "text/html", "x-frame-options": "DENY"}
    site.pages["http://anysite.com/"] = (200, INDEX, {"content-type": "text/html"})
    result = check(client)
    assert kinds(result) == [("look", "no_https_redirect"), ("minor", "missing_headers")]
    redirect, headers = result["security"]["findings"]
    assert redirect["plain"] == ("People who type http://anysite.com get your site without a secure connection, "
                                 "instead of being moved to the secure address.")
    assert "301 or 308" in redirect["engineer"]
    assert headers["missing"] == ["Strict-Transport-Security", "Content-Security-Policy",
                                  "X-Content-Type-Options: nosniff"]
    assert redirect["plain"] in result["worth_fixing"] and headers["plain"] in result["worth_fixing"]
    assert "For your engineer:" in result["text"] and "Strict-Transport-Security" in result["text"]
    assert result["security"]["https_redirect"] is False


def test_a_redirect_through_www_counts_but_one_to_another_site_says_nothing(client, site):
    a_tidy_site(site)
    site.pages["http://anysite.com/"] = (301, "", {"location": "http://www.anysite.com/"})
    site.pages["http://www.anysite.com/"] = (301, "", {"location": "https://www.anysite.com/"})
    assert check(client)["security"]["https_redirect"] is True

    site.pages["http://anysite.com/"] = (301, "", {"location": "http://parked.example/"})
    result = check(client)
    assert result["security"]["https_redirect"] is None
    assert "http://parked.example/" not in site.asked
    assert "no_https_redirect" not in [kind for _, kind in kinds(result)]


def test_cookies_without_secure_or_httponly_are_flagged_by_name_only(client, site):
    a_tidy_site(site)
    site.headers = [*SAFE.items(), ("set-cookie", "session_id=abc123secret; Path=/"),
                    ("set-cookie", "_ga=GA1.1.99; Path=/; Secure"), ("set-cookie", "theme=dark; Path=/")]
    result = check(client)
    assert kinds(result) == [("minor", "cookie_flags")]
    finding = result["security"]["findings"][0]
    assert finding["cookies"] == [{"name": "session_id", "missing": ["Secure", "HttpOnly"]},
                                  {"name": "theme", "missing": ["Secure"]}]
    assert "abc123secret" not in json.dumps(result)


def test_a_leaked_nextjs_version_is_matched_against_osv(client, site):
    a_tidy_site(site)
    site.headers["x-powered-by"] = "Next.js 14.1.0"
    site.osv = {"/v1/query": {"vulns": [
        {"id": "GHSA-f82v-jwr5-mffw", "aliases": ["CVE-2025-29927"], "summary": "Authorization bypass in middleware.",
         "database_specific": {"severity": "CRITICAL"},
         "affected": [{"package": {"name": "next", "ecosystem": "npm"}, "ranges": [{"events": [
             {"introduced": "13.0.0"}, {"fixed": "13.5.9"}, {"introduced": "14.0.0"}, {"fixed": "14.2.25"}]}]}]},
        # The same problem again under its other id: counted once.
        {"id": "CVE-2025-29927", "aliases": ["GHSA-f82v-jwr5-mffw"], "affected": []},
        {"id": "GHSA-low", "summary": "Minor cache issue.", "database_specific": {"severity": "LOW"},
         "affected": [{"package": {"name": "next"}, "ranges": [{"events": [{"fixed": "14.1.1"}]}]}]},
    ]}}
    result = check(client)
    assert kinds(result) == [("look", "known_vulnerable_version")]
    finding = result["security"]["findings"][0]
    assert finding["plain"] == ("Your site says it runs Next.js 14.1.0, which has 2 known security problems "
                                "(1 rated high or critical). Upgrading to 14.2.25 or later fixes all of them.")
    assert finding["engineer"].startswith("The x-powered-by header shows Next.js 14.1.0 (npm package next). "
                                          "OSV.dev lists 2 advisories for it, including GHSA-f82v-jwr5-mffw "
                                          "(critical, fixed in 14.2.25).")
    assert finding["fixed"] == "14.2.25"
    assert finding["advisories"] == [{"id": "GHSA-f82v-jwr5-mffw", "severity": "critical", "fixed": "14.2.25"},
                                     {"id": "GHSA-low", "severity": "low", "fixed": "14.1.1"}]
    assert result["security"]["software"] == [{"name": "Next.js", "version": "14.1.0", "where": "x-powered-by header"}]
    assert "Tells visitors it runs Next.js 14.1.0 (in its x-powered-by header)." in result["found"]


def test_a_generator_tag_is_read_and_a_slow_or_unknown_lookup_is_left_out(client, site):
    a_tidy_site(site)
    site.body = '<html><head><meta name="generator" content="WordPress 6.4.2"></head></html>'
    site.headers["x-powered-by"] = "PHP/8.1.2"
    result = check(client)  # nothing to look either up in: no OSV request, no finding
    assert result["security"]["software"] == [
        {"name": "PHP", "version": "8.1.2", "where": "x-powered-by header"},
        {"name": "WordPress", "version": "6.4.2", "where": "generator meta tag"}]
    assert result["security"]["findings"] == []

    site.body = '<meta content="Astro v4.0.0" name="generator">'
    site.osv = {}  # the lookup fails: the check still answers
    result = check(client)
    assert result["security"]["software"][-1] == {"name": "Astro", "version": "4.0.0", "where": "generator meta tag"}
    assert result["security"]["findings"] == []
