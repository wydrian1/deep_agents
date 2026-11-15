"""Public-web tools with SSRF protection.

Rules: HTTPS on port 443 only, no credentials in the URL, no cookies/auth headers, every
resolved address must be globally routable, the connection is pinned to the checked
address, every redirect is re-validated (max 3), responses are capped at 2 MB, and the
gateway/database hosts plus WEB_BLOCKED_HOSTS are always denied.

Not an OS sandbox: it only protects what the app itself can reach over HTTP.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from defusedxml import ElementTree

from ..config import Settings
from .registry import ToolContext, register_tool

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TOTAL_SECONDS = 20.0
USER_AGENT = "DeepAgentsApp/0.1"


class WebDenied(Exception):
    pass


class WebUnavailable(Exception):
    pass


def validate_public_url(raw: object, settings: Settings) -> tuple[str, str]:
    """Return (host, path_and_query) for an allowed URL or raise WebDenied."""
    if not isinstance(raw, str) or not raw or len(raw) > 2000 or re.search(r"[\x00-\x20]", raw):
        raise WebDenied
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().strip(".")
    try:
        port = parts.port
    except ValueError:
        raise WebDenied from None
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        raise WebDenied
    for blocked in settings.blocked_host_suffixes:
        if host == blocked or host.endswith("." + blocked):
            raise WebDenied
    path = parts.path or "/"
    return host, path + ("?" + parts.query if parts.query else "")


def is_public_address(value: str) -> bool:
    """Standard "globally routable" AND the previous system's explicit block list (the stricter of both).

    IPv4: not 0/8, 10/8, 127/8, 100.64/10, 169.254/16, 172.16/12, 192.0/16, 192.168/16, 198.18/15, 224+.
    IPv6: global unicast 2000::/3 only (so IPv4-mapped, loopback, link-local and unique-local are denied),
    except the 2001:db8::/32 documentation range.
    """
    address = ipaddress.ip_address(value.split("%")[0])
    if isinstance(address, ipaddress.IPv4Address):
        a, b = address.packed[0], address.packed[1]
        blocked = (
            a in (0, 10, 127) or a >= 224 or (a == 169 and b == 254) or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b in (168, 0)) or (a == 100 and 64 <= b <= 127) or (a == 198 and b in (18, 19))
        )
        return not blocked and address.is_global
    in_global_unicast = address in ipaddress.IPv6Network("2000::/3") and address not in ipaddress.IPv6Network("2001:db8::/32")
    return in_global_unicast and address.is_global and not address.is_multicast


def resolve_public(host: str) -> str:
    def lookup() -> list[str]:
        return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            addresses = pool.submit(lookup).result(timeout=5)
        except (FutureTimeout, OSError):
            raise WebUnavailable from None
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise WebDenied
    return addresses[0]


def download(
    url: str, settings: Settings, *, accept: str = "text/html, application/json, application/xml, text/plain",
    transport: httpx.BaseTransport | None = None, depth: int = 0, deadline: float | None = None,
) -> str:
    # One overall time budget for the whole download including redirects (previous system: 20 s).
    deadline = deadline or time.monotonic() + TOTAL_SECONDS
    host, target = validate_public_url(url, settings)
    ip = resolve_public(host)
    netloc = f"[{ip}]" if ":" in ip else ip
    headers = {"Host": host, "User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "identity"}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebUnavailable
        with httpx.Client(timeout=min(15.0, remaining), follow_redirects=False, transport=transport, trust_env=False) as client:
            with client.stream(
                "GET", f"https://{netloc}{target}", headers=headers, extensions={"sni_hostname": host}
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location or depth >= MAX_REDIRECTS:
                        raise WebUnavailable
                    return download(
                        urljoin(url, location), settings, accept=accept, transport=transport, depth=depth + 1, deadline=deadline
                    )
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                    raise WebUnavailable
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES or time.monotonic() > deadline:
                        raise WebUnavailable
                return body.decode("utf-8", errors="replace")
    except httpx.HTTPError:
        raise WebUnavailable from None


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "iframe", "form", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def html_text(body: str, limit: int) -> str:
    parser = _TextExtractor()
    parser.feed(body)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def search_results(body: str, settings: Settings) -> list[dict[str, str]]:
    if re.search(r"<!DOCTYPE|<!ENTITY", body, re.I):
        raise WebUnavailable
    root = ElementTree.fromstring(body, forbid_dtd=True)
    results = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        try:
            validate_public_url(link, settings)
        except WebDenied:
            continue
        results.append(
            {
                "title": (item.findtext("title") or "")[:300],
                "url": link,
                "snippet": html_text(item.findtext("description") or "", 1000),
            }
        )
        if len(results) == 8:
            break
    if not results:
        raise WebUnavailable
    return results


@register_tool(
    "webfetch", label="Web fetch", access="read",
    description="Read a public HTTPS page. No credentials; private networks and our own backends are denied.",
)
def webfetch_tools(ctx: ToolContext) -> list:
    def webfetch(url: str) -> dict[str, Any]:
        """Read a public HTTPS page without credentials and return its text. Private networks are denied."""
        try:
            body = download(url, ctx.settings)
        except WebDenied:
            return {"error": ctx.t("web_denied")}
        except WebUnavailable:
            return {"error": ctx.t("web_unavailable")}
        return {"url": url, "text": html_text(body, 32000) or body[:32000]}

    return [webfetch]


@register_tool(
    "websearch", label="Web search", access="read",
    description="Search the public web (Bing RSS, up to 8 results). Fails explicitly when unavailable.",
)
def websearch_tools(ctx: ToolContext) -> list:
    def websearch(query: str) -> dict[str, Any]:
        """Search the public web. Returns up to 8 results; reports an error instead of inventing results."""
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            return {"error": ctx.t("tool_bad_input")}
        try:
            body = download("https://www.bing.com/search?format=rss&q=" + quote(query), ctx.settings)
            return {"provider": "Bing RSS", "results": search_results(body, ctx.settings)}
        except (WebDenied, WebUnavailable, ElementTree.ParseError):
            return {"error": ctx.t("search_unavailable")}

    return [websearch]
