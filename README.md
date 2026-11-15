# Deep Agents service

A standalone, Docker-runnable [Deep Agents](https://github.com/langchain-ai/deepagents) service:
FastAPI + LangGraph, an **external Postgres** for all state, and **one LiteLLM gateway** for every model.
It is a development foundation, not a finished product (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

> **Security status:** by default there is **no login and no access protection**. Run it only on a
> trusted network. The compose file publishes the port on `127.0.0.1` only. A plain OAuth login is
> implemented but **disabled** (`AUTH_ENABLED=false`).

## What you get

- Runs a Deep Agent per request (`POST /api/runs`), streams its events, stores the result.
- State in Postgres (own schema, Alembic migrations, LangGraph `PostgresSaver` checkpoints).
- Models through a LiteLLM gateway: `LLM_GATEWAY_URL` + `LLM_API_KEY`, model = the gateway alias.
- General tools, opt-in per run: `webfetch`, `websearch`, `github` (public, read-only), `files`,
  `document` (PDF/DOCX/PPTX/XLSX/text), `report` (private md/txt/csv/xlsx/pdf outputs).
- Configurable language (`APP_LANGUAGE`: `en`, `hu`; add more as data files).
- A LangGraph-SDK-compatible read API and a minimal developer page at `/`.
- Optional OAuth 2.0 (authorization code + PKCE) login, off by default.

## Run it

```bash
cp .env.example .env        # fill in DATABASE_URL, LLM_GATEWAY_URL, LLM_API_KEY, DEFAULT_MODEL
docker compose up --build   # app on http://127.0.0.1:8000
```

The database is **not** part of the stack: point `DATABASE_URL` at your Postgres. To try it locally with a
throwaway Postgres, use the `dev` profile and `DATABASE_URL=postgresql://deep_agents:deep_agents@postgres:5432/deep_agents`:

```bash
docker compose --profile dev up --build
```

Smoke test without any gateway (offline fake model, still uses Postgres and the real agent engine):

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs -H 'content-type: application/json' \
     -d '{"prompt":"hello","mode":"test"}'
```

Interactive API docs: `http://127.0.0.1:8000/api/docs`.

## Configuration

All settings are environment variables; see [.env.example](.env.example) for the full, commented list.

| Area | Variables |
| --- | --- |
| Database | `DATABASE_URL`, `DATABASE_SCHEMA`, `AUTO_MIGRATE` |
| Gateway | `LLM_GATEWAY_URL`, `LLM_API_KEY`, `DEFAULT_MODEL`, `LLM_PARAMS_JSON`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` |
| Behaviour | `APP_LANGUAGE`, `DATA_DIR`, `MAX_CONCURRENT_RUNS`, `RUN_TIMEOUT_SECONDS`, `ALLOW_TEST_MODE`, `WEB_BLOCKED_HOSTS` |
| OAuth login (off) | `AUTH_ENABLED`, `OAUTH_*`, `SESSION_SECRET`, `COOKIE_SECURE` |

### Enabling the OAuth login later

Set `AUTH_ENABLED=true` and provide `OAUTH_AUTHORIZE_URL`, `OAUTH_TOKEN_URL`, `OAUTH_USERINFO_URL`,
`OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URL` (`https://<host>/auth/callback`),
`OAUTH_ALLOWED_EMAILS` (comma separated; nobody else may log in) and a random `SESSION_SECRET` (≥ 32 chars).
The service refuses to start with an incomplete configuration. Serve it over HTTPS (keep `COOKIE_SECURE=true`).
`/api/*` then needs a session; `/healthz` and the static page stay open.

## Development

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker run -d --name da-test-pg -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
    -p 127.0.0.1:55432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/test
ruff check . && mypy src && pytest
```

Tests that need Postgres are skipped when `TEST_DATABASE_URL` is unset. Contributor rules: [AGENTS.md](AGENTS.md).
