# Working agreement

This repository is public. Treat everything committed here as published.

## Workflow

1. Inspect the working tree before starting; never discard or overwrite unrelated changes.
2. Fetch `origin`, then create a descriptive task branch from the current `origin/main`
   (`feat/<topic>`, `fix/<topic>`, `chore/<topic>`). Do not work directly on `main`.
   Continuing the same task reuses its branch and PR.
3. Make only the requested change, following the existing structure.
4. After the **final edit**, run all of these and fix failures before committing:
   - `ruff check .`
   - `mypy src`
   - `pytest` with `TEST_DATABASE_URL` pointing at a throwaway Postgres (see README). A run where the
     database tests were skipped does not count.
   - `docker build -t deep-agents-app:test .` for any change that touches dependencies, the Dockerfile,
     compose, or application startup.
5. Review the diff and the exact staged files for secrets and unrelated changes, then commit, push the
   branch and open a PR against `main` with a short summary and the real commands/results you ran.
6. Merge only when the owner asks. Never push directly to `main`, force-push or delete branches without
   explicit authorization.

## Rules that protect the project

- **No secrets, ever.** `.env`, API keys, gateway keys, OAuth client secrets, session secrets, database
  URLs with passwords, tokens and private data must not appear in commits, PRs, logs or test fixtures.
  Only `.env.example` with placeholders is tracked. Keep the `.gitignore` protections.
- **Database schema changes only through Alembic migrations** (`src/deep_agents_app/migrations/versions`).
  Update `db/schema.py` in the same change. Never edit an applied migration; add a new one.
- **Models are built only in `llm/gateway.py`.** No other module imports a provider SDK or reads the API key.
- **Auth goes through `auth.current_principal`.** New endpoints that act for a caller must depend on it.
  `AUTH_ENABLED` stays `false` by default.
- **Errors shown to users or stored in the database are message keys** (`messages/*.json`), never raw
  exception text or model/tool content. Add new keys to every language file (a test enforces this).
- **Tools** are registered in `tools/registry.py`, opt-in per run, audited (operation, outcome, duration only)
  and must fail with an explicit error instead of inventing results. Web access must go through
  `tools/web.py` (SSRF protection); do not add a second HTTP path to the open internet.
- No automatic retries at run, tool or scheduler level; at most one model-request retry before a response starts.
- Keep user-facing text out of code: prompts live in `prompts/<lang>.md`, messages in `messages/<lang>.json`.
- Deployment and production data are out of scope for source changes; do not add deployment secrets or
  environment-specific hosts to the repository.
