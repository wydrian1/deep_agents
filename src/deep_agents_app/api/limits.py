"""Request body size limit for JSON endpoints (the previous system rejected bodies over 18,000 bytes).

File uploads (``POST /api/files``) have their own 2 MB limit and are exempt. Counting happens on the byte
stream, so it also covers requests without a Content-Length header.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from ..i18n import message

MAX_JSON_BODY = 18_000
_Scope = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
_Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class BodyLimitMiddleware:
    def __init__(self, app: Callable[[_Scope, _Receive, _Send], Awaitable[None]], language: str):
        self.app, self.language = app, language

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        limited = (
            scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH") and scope["path"] != "/api/files"
        )
        if not limited:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > MAX_JSON_BODY:
            await self._reject(send)
            return
        seen, rejected = 0, False

        async def counting_receive() -> MutableMapping[str, Any]:
            nonlocal seen, rejected
            event = await receive()
            if event["type"] == "http.request":
                seen += len(event.get("body", b""))
                if seen > MAX_JSON_BODY:
                    rejected = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return event

        started = False

        async def guarded_send(event: MutableMapping[str, Any]) -> None:
            nonlocal started
            if rejected and not started and event["type"] == "http.response.start":
                started = True
                await self._reject(send)
                return
            if rejected and started:
                return  # swallow the downstream response; the 413 was already sent
            await send(event)

        await self.app(scope, counting_receive, guarded_send)

    async def _reject(self, send: _Send) -> None:
        body = json.dumps({"detail": message("body_too_large", self.language)}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
