"""Read-only access to PUBLIC GitHub repositories through the unauthenticated REST API."""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote

from .registry import ToolContext, register_tool
from .web import WebDenied, WebUnavailable, download

_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def github_url(repository: str, ref: str, path: str | None) -> str:
    parts = repository.split("/") if isinstance(repository, str) else []
    if len(parts) != 2 or any(not _PART.match(p) or p in (".", "..") for p in parts):
        raise ValueError("repository")
    if not isinstance(ref, str) or not ref or len(ref) > 200 or re.search(r"[\x00-\x20]", ref):
        raise ValueError("ref")
    base = "https://api.github.com/repos/" + repository
    if path is None:
        return f"{base}/git/trees/{quote(ref, safe='')}?recursive=1"
    segments = path.split("/") if isinstance(path, str) else []
    if not path or len(path) > 500 or any(not s or s in (".", "..") for s in segments) or re.search(r"[\\\x00-\x1f]", path):
        raise ValueError("path")
    return f"{base}/contents/{'/'.join(quote(s, safe='') for s in segments)}?ref={quote(ref, safe='')}"


@register_tool(
    "github", label="GitHub (public, read-only)", access="read",
    description="Read the file tree or a file of a public GitHub repository. Anonymous: no tokens, no writes.",
)
def github_tools(ctx: ToolContext) -> list:
    def github(repository: str, ref: str = "HEAD", path: str = "") -> dict[str, Any]:
        """Read a public GitHub repository tree, or one UTF-8 file when path is given. repository is owner/name."""
        try:
            url = github_url(repository, ref, path or None)
            body = json.loads(download(url, ctx.settings, accept="application/json"))
            if not path:
                if not isinstance(body.get("tree"), list) or body.get("truncated"):
                    raise WebUnavailable
                files = [
                    {"path": i["path"], "size": i.get("size")} for i in body["tree"] if i.get("type") == "blob"
                ][:1000]
                return {"sha": body.get("sha"), "files": files}
            if body.get("type") != "file" or body.get("encoding") != "base64" or body.get("size", 0) > 1024 * 1024:
                raise WebUnavailable
            text = base64.b64decode(body["content"]).decode("utf-8", errors="replace")[:64000]
            return {"sha": body.get("sha"), "path": body.get("path"), "text": text}
        except json.JSONDecodeError:
            return {"error": ctx.t("web_unavailable")}
        except ValueError:
            return {"error": ctx.t("tool_bad_input")}
        except WebDenied:
            return {"error": ctx.t("web_denied")}
        except (WebUnavailable, KeyError, TypeError, AttributeError):
            return {"error": ctx.t("web_unavailable")}

    return [github]
