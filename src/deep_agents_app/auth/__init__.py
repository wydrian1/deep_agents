"""Authentication seam.

Every route that acts on behalf of a caller depends on :func:`current_principal`.

* ``AUTH_ENABLED=false`` (the default): there is NO login and NO access protection; every
  caller is the local principal. The service must then only be reachable from a trusted
  network (the compose file publishes it on 127.0.0.1 only).
* ``AUTH_ENABLED=true``: a plain OAuth 2.0 authorization-code + PKCE login (see
  :mod:`deep_agents_app.auth.oauth`) protects ``/api/*``; only allow-listed e-mail
  addresses get a session.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..config import Settings


@dataclass(frozen=True)
class Principal:
    id: str
    name: str = ""
    email: str = ""


LOCAL = Principal(id="local", name="Local user")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def current_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return LOCAL
    from . import oauth

    principal = oauth.session_principal(request, settings)
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    # Cookie-authenticated, state-changing requests must come from our own origin (CSRF).
    if request.method not in _SAFE_METHODS and request.headers.get("origin") != oauth.app_origin(settings):
        raise HTTPException(status_code=403, detail="cross-origin request denied")
    return principal
