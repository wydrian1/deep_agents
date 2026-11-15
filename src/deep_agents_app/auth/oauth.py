"""Plain OAuth 2.0 authorization-code login with PKCE (works with any OIDC-style provider).

Provider endpoints are configured explicitly (no discovery magic). After the code
exchange the access token is used ONCE against the userinfo endpoint; the e-mail
address must be verified (if the provider says so) and on the allow-list. The session
is a stateless signed cookie (HMAC-SHA256, expiring). Mounted only when AUTH_ENABLED=true.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ..config import Settings
from . import Principal

SESSION_COOKIE = "da_session"
FLOW_COOKIE = "da_oauth_flow"
FLOW_TTL = 600


def app_origin(settings: Settings) -> str:
    parts = urlsplit(settings.oauth_redirect_url)
    return f"{parts.scheme}://{parts.netloc}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(settings: Settings, payload: dict[str, Any], ttl: int) -> str:
    body = _b64(json.dumps({**payload, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    mac = hmac.new(settings.session_secret.get_secret_value().encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(settings: Settings, token: str | None) -> dict[str, Any] | None:
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    expected = hmac.new(settings.session_secret.get_secret_value().encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def session_principal(request: Request, settings: Settings) -> Principal | None:
    payload = verify(settings, request.cookies.get(SESSION_COOKIE))
    if not payload or payload.get("email", "").lower() not in settings.allowed_emails:
        return None  # an allow-list change takes effect immediately
    return Principal(id=str(payload.get("sub") or payload["email"]), name=str(payload.get("name", "")), email=payload["email"])


def _set_cookie(response: Response, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/"
    )


def router(settings: Settings) -> APIRouter:
    api = APIRouter(prefix="/auth", tags=["auth"])

    @api.get("/login")
    def login() -> Response:
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.oauth_client_id,
                "redirect_uri": settings.oauth_redirect_url,
                "scope": settings.oauth_scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(f"{settings.oauth_authorize_url}?{query}", status_code=302)
        _set_cookie(response, settings, FLOW_COOKIE, sign(settings, {"state": state, "verifier": verifier}, FLOW_TTL), FLOW_TTL)
        return response

    @api.get("/callback")
    def callback(request: Request, code: str = "", state: str = "") -> Response:
        flow = verify(settings, request.cookies.get(FLOW_COOKIE))
        if not flow or not code or not state or not hmac.compare_digest(str(flow.get("state", "")), state):
            raise HTTPException(400, "invalid login state")
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                token = client.post(
                    settings.oauth_token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": settings.oauth_redirect_url,
                        "client_id": settings.oauth_client_id,
                        "client_secret": settings.oauth_client_secret.get_secret_value(),
                        "code_verifier": flow["verifier"],
                    },
                    headers={"Accept": "application/json"},
                )
                token.raise_for_status()
                access_token = token.json()["access_token"]
                info = client.get(settings.oauth_userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
                info.raise_for_status()
                profile = info.json()
        except (httpx.HTTPError, KeyError, ValueError):
            raise HTTPException(502, "login provider error") from None
        email = str(profile.get("email", "")).strip().lower()
        if not email or profile.get("email_verified") is False or email not in settings.allowed_emails:
            raise HTTPException(403, "this account is not allowed")
        session = sign(
            settings,
            {"sub": str(profile.get("sub", email)), "email": email, "name": str(profile.get("name", ""))[:100]},
            settings.session_ttl_seconds,
        )
        response = RedirectResponse("/", status_code=302)
        _set_cookie(response, settings, SESSION_COOKIE, session, settings.session_ttl_seconds)
        response.delete_cookie(FLOW_COOKIE, path="/")
        return response

    @api.post("/logout")
    def logout(request: Request) -> Response:
        if request.headers.get("origin") != app_origin(settings):
            raise HTTPException(403, "cross-origin request denied")
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @api.get("/me")
    def me(request: Request) -> dict[str, Any]:
        principal = session_principal(request, settings)
        if principal is None:
            raise HTTPException(401, "authentication required")
        return {"id": principal.id, "email": principal.email, "name": principal.name}

    return api
