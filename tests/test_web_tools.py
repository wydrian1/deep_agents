import base64
import json

import httpx
import pytest

from deep_agents_app.config import Settings
from deep_agents_app.tools import web
from deep_agents_app.tools.github import github_url

SETTINGS = Settings(  # type: ignore[call-arg]
    _env_file=None, database_url="postgresql://u:p@db.internal/x", llm_gateway_url="http://litellm.internal:4000/v1"
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/", "https://user:pw@example.com/", "https://example.com:8443/", "ftp://example.com/",
        "https://example.com/a b", "https://litellm.internal/v1", "https://db.internal/", "https://x.db.internal/",
        "", "https:///path", "https://example.com/" + "a" * 2000,
    ],
)
def test_url_policy_denies(url):
    with pytest.raises(web.WebDenied):
        web.validate_public_url(url, SETTINGS)


def test_url_policy_allows_public_https():
    assert web.validate_public_url("https://Example.com/a?b=1", SETTINGS) == ("example.com", "/a?b=1")


@pytest.mark.parametrize(
    ("address", "public"),
    [("93.184.216.34", True), ("10.0.0.5", False), ("127.0.0.1", False), ("169.254.169.254", False),
     ("192.168.1.1", False), ("::1", False), ("fe80::1", False), ("::ffff:10.0.0.1", False), ("fd00::1", False)],
)
def test_public_address_check(address, public):
    assert web.is_public_address(address) is public


def fake_resolver(monkeypatch, mapping):
    def resolve(host):
        if host not in mapping:
            raise web.WebDenied
        return mapping[host]

    monkeypatch.setattr(web, "resolve_public", resolve)


def test_download_pins_the_checked_address(monkeypatch):
    fake_resolver(monkeypatch, {"example.com": "93.184.216.34"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(host=request.url.host, header=request.headers["host"], enc=request.headers["accept-encoding"])
        return httpx.Response(200, text="<html><body><script>x()</script>Hello <b>world</b></body></html>")

    body = web.download("https://example.com/", SETTINGS, transport=httpx.MockTransport(handler))
    assert seen == {"host": "93.184.216.34", "header": "example.com", "enc": "identity"}
    assert web.html_text(body, 100) == "Hello world"


def test_redirects_are_revalidated_and_limited(monkeypatch):
    fake_resolver(monkeypatch, {"example.com": "93.184.216.34"})  # internal.test does not resolve publicly
    redirect = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://internal.test/"}))
    with pytest.raises(web.WebDenied):
        web.download("https://example.com/", SETTINGS, transport=redirect)
    loop = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://example.com/again"}))
    with pytest.raises(web.WebUnavailable):
        web.download("https://example.com/", SETTINGS, transport=loop)


def test_oversized_and_error_responses_are_refused(monkeypatch):
    fake_resolver(monkeypatch, {"example.com": "93.184.216.34"})
    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (web.MAX_BYTES + 1)))
    with pytest.raises(web.WebUnavailable):
        web.download("https://example.com/", SETTINGS, transport=big)
    bad = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(web.WebUnavailable):
        web.download("https://example.com/", SETTINGS, transport=bad)


def test_search_results_drop_private_links_and_dtds():
    rss = (
        "<rss><channel>"
        "<item><title>ok</title><link>https://example.com/a</link><description>&lt;p&gt;snip&lt;/p&gt;</description></item>"
        "<item><title>bad</title><link>https://db.internal/x</link></item>"
        "</channel></rss>"
    )
    assert web.search_results(rss, SETTINGS) == [{"title": "ok", "url": "https://example.com/a", "snippet": "snip"}]
    with pytest.raises(web.WebUnavailable):
        web.search_results('<!DOCTYPE x [<!ENTITY a "b">]><rss/>', SETTINGS)
    with pytest.raises(web.WebUnavailable):
        web.search_results("<rss><channel/></rss>", SETTINGS)


def test_github_url_validation():
    assert github_url("a/b", "main", None).endswith("/git/trees/main?recursive=1")
    assert "/contents/dir/f%20x.py?ref=v1" in github_url("a/b", "v1", "dir/f x.py")
    for repo, ref, path in [("a", "x", None), ("a/../b", "x", None), ("a/b", "", None), ("a/b", "x", "../etc"), ("a/b", "x", "a//b")]:
        with pytest.raises(ValueError):
            github_url(repo, ref, path)


def test_github_tool_reads_a_file(monkeypatch, settings):
    from deep_agents_app.tools import github as gh
    from deep_agents_app.tools.registry import ToolContext

    payload = {"type": "file", "encoding": "base64", "size": 5, "sha": "s", "path": "a.txt",
               "content": base64.b64encode(b"hello").decode()}
    monkeypatch.setattr(gh, "download", lambda url, s, accept="": json.dumps(payload))
    (tool,) = gh.github_tools(ToolContext(settings, None, "r", "en"))  # type: ignore[arg-type]
    assert tool("a/b", "main", "a.txt")["text"] == "hello"
    assert "error" in tool("not-a-repo")
