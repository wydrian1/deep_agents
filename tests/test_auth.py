from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from deep_agents_app.auth import oauth
from deep_agents_app.main import create_app

from .conftest import make_settings

ORIGIN = "https://app.example"


@pytest.fixture
def secured(database_url, tmp_path):
    settings = make_settings(
        database_url, "t_auth_x3", tmp_path,
        auth_enabled=True, oauth_authorize_url="https://idp.example/authorize", oauth_token_url="https://idp.example/token",
        oauth_userinfo_url="https://idp.example/userinfo", oauth_client_id="client", oauth_client_secret="secret",
        oauth_redirect_url=f"{ORIGIN}/auth/callback", oauth_allowed_emails="Owner@Example.com",
        session_secret="x" * 40, cookie_secure=False,
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        yield client
    engine = create_engine(settings.sqlalchemy_url)
    with engine.begin() as db:
        db.execute(text('DROP SCHEMA IF EXISTS "t_auth_x3" CASCADE'))
    engine.dispose()


def provider(monkeypatch, profile):
    real, calls = httpx.Client, []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=profile)

    monkeypatch.setattr(oauth.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


def begin(client):
    redirect = client.get("/auth/login")
    assert redirect.status_code == 302
    return parse_qs(urlsplit(redirect.headers["location"]).query)


def test_protected_routes_need_a_session_but_health_and_index_do_not(secured):
    assert secured.get("/healthz").status_code == 200 and secured.get("/").status_code == 200
    for path in ("/api/runs", "/api/tools", "/api/langgraph/threads/x/state"):
        assert secured.get(path).status_code == 401
    assert secured.post("/api/runs", json={"prompt": "x"}).status_code == 401
    assert secured.get("/auth/me").status_code == 401


def test_login_redirect_uses_authorization_code_with_pkce(secured):
    query = begin(secured)
    assert query["response_type"] == ["code"] and query["client_id"] == ["client"]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) == 43
    assert query["redirect_uri"] == [f"{ORIGIN}/auth/callback"] and query["state"][0]


def test_full_login_flow_then_csrf_and_logout(secured, monkeypatch):
    calls = provider(monkeypatch, {"sub": "u1", "email": "owner@example.com", "email_verified": True, "name": "Own"})
    state = begin(secured)["state"][0]
    done = secured.get("/auth/callback", params={"code": "abc", "state": state})
    assert done.status_code == 302 and done.headers["location"] == "/"
    token_call = next(c for c in calls if c.url.path == "/token")
    body = parse_qs(token_call.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code_verifier"][0] and body["client_secret"] == ["secret"]

    assert secured.get("/auth/me").json() == {"id": "u1", "email": "owner@example.com", "name": "Own"}
    assert secured.get("/api/tools").status_code == 200
    # State-changing calls must come from the app's own origin.
    assert secured.post("/api/runs", json={"prompt": "x", "mode": "test"}).status_code == 403
    assert secured.post("/api/runs", json={"prompt": "x", "mode": "test"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert secured.post("/api/runs", json={"prompt": "x", "mode": "test"}, headers={"origin": ORIGIN}).status_code == 201

    assert secured.post("/auth/logout").status_code == 403
    assert secured.post("/auth/logout", headers={"origin": ORIGIN}).status_code == 204
    assert secured.get("/api/tools").status_code == 401


@pytest.mark.parametrize(
    "profile",
    [{"email": "stranger@example.com"}, {"email": "owner@example.com", "email_verified": False}, {"sub": "no-email"}],
)
def test_accounts_off_the_allow_list_or_unverified_are_rejected(secured, monkeypatch, profile):
    provider(monkeypatch, profile)
    state = begin(secured)["state"][0]
    assert secured.get("/auth/callback", params={"code": "abc", "state": state}).status_code == 403
    assert secured.get("/api/tools").status_code == 401


def test_bad_state_and_tampered_sessions_are_rejected(secured, monkeypatch):
    provider(monkeypatch, {"email": "owner@example.com"})
    begin(secured)
    assert secured.get("/auth/callback", params={"code": "abc", "state": "forged"}).status_code == 400
    assert secured.get("/auth/callback", params={"state": "x"}).status_code == 400
    secured.cookies.set(oauth.SESSION_COOKIE, "e30.AAAA")
    assert secured.get("/api/tools").status_code == 401


def test_sessions_expire_and_follow_the_allow_list(secured):
    settings = secured.app.state.settings
    expired = oauth.sign(settings, {"email": "owner@example.com"}, -5)
    assert oauth.verify(settings, expired) is None
    forged = oauth.sign(settings.model_copy(update={"session_secret": settings.session_secret.__class__("y" * 40)}), {"email": "owner@example.com"}, 60)
    assert oauth.verify(settings, forged) is None
    removed = oauth.sign(settings, {"email": "someone-removed@example.com"}, 60)
    secured.cookies.set(oauth.SESSION_COOKIE, removed)
    assert secured.get("/api/tools").status_code == 401


def test_auth_routes_do_not_exist_when_disabled(client):
    assert client.get("/auth/login").status_code == 404
