# Architecture

## Goals and non-goals

A standalone service that runs Deep Agents in Docker with an **external Postgres**, **one LiteLLM gateway**
for models, no dashboard dependency and, for now, no login. Phase 1 is a foundation to keep developing,
not a hardened product.

```
client ──HTTP──▶ FastAPI (api/) ──▶ RunStore (Postgres: runs, run_events, audit, files, artifacts)
                    │                    ▲
                    ▼                    │ events / state / cancel polling
              Supervisor (≤ MAX_CONCURRENT_RUNS, kills on cancel/timeout)
                    │ one process per run
                    ▼
        worker → Deep Agent (agents/runner.py) ──▶ LiteLLM gateway (llm/gateway.py)
                    │                  └▶ tools/ (web, github, files, documents, reports)
                    ▼
        LangGraph PostgresSaver (checkpoints, same database, same schema)
```

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Every setting, validated; the only place that reads the environment. |
| `llm/gateway.py` | **Only** place that builds chat models (`ChatOpenAI` against the LiteLLM `/v1` URL) plus the offline test model. |
| `db/` | SQLAlchemy engine (own schema via `search_path`), table definitions, programmatic Alembic upgrade guarded by an advisory lock, checkpointer setup. |
| `migrations/` | Alembic revisions. The schema changes only here. |
| `runs/store.py` | Run lifecycle on Postgres: slot reservation, events, audit, files, artifacts. |
| `runs/events.py` | JSON projection of LangGraph stream output; message-delta coalescing. |
| `agents/runner.py` | Builds the Deep Agent, streams it, persists events and the result (`execute_run`). |
| `agents/supervisor.py` | One killable worker process per run: kills the process tree on cancel, timeout or shutdown; no automatic restart. `RUN_MODE=thread` runs in-process for tests. |
| `agents/worker.py` | Worker entry point (`python -m deep_agents_app.agents.worker <run-id>`): runs one run, then exits. |
| `runs/metrics.py` | LangChain callback writing one content-free `llm_calls` row per model call (model, status, duration, tokens). Never alters or fails a run. |
| `api/routes.py` | `/api/runs`, `/api/files`, `/api/tools`, artifact download, `/api/runs/{id}/metrics`, `/api/metrics/summary`. |
| `api/langgraph.py` | Narrow LangGraph-SDK-compatible read API (thread state, SSE join, cancel). |
| `auth/` | `current_principal` seam; optional OAuth 2.0 + PKCE login (off by default). |
| `tools/` | Registry and tools. Web access only through `tools/web.py`. |
| `i18n.py`, `prompts/`, `messages/` | Language packs (`APP_LANGUAGE`, per-run `language`). |

## Key decisions

- **Postgres, external.** `DATABASE_URL` only; the compose file has no database (a `dev` profile adds a throwaway
  one). All state lives in the app's own schema (`DATABASE_SCHEMA`). Migrations run on startup under a
  session-level advisory lock, so several replicas can start together.
- **Concurrency limit across replicas.** Slots are reserved in a transaction holding `pg_advisory_xact_lock`,
  counting `running` rows, so `MAX_CONCURRENT_RUNS` holds for the whole deployment. Orphaned reservations
  (older than timeout + 60 s) are marked failed, never re-submitted.
- **Gateway, not providers.** The model name is the LiteLLM alias; extra request parameters go through
  `LLM_PARAMS_JSON`. The API key is a `SecretStr`, used only inside `llm/gateway.py`. If the final gateway is not
  OpenAI-compatible, replace that one module.
- **No automatic retries** at run/tool level; one model-request retry before a response starts
  (`LLM_MAX_RETRIES`). Failures are stored as **message keys** and localized when read.
- **Events.** Stream output is stored in `run_events` (bounded per run: 20,000 events / 24 MB, 1 MB each) and
  served as SSE with a durable cursor (`Last-Event-ID`). Polling today; `LISTEN/NOTIFY` is a drop-in later.
- **Auth seam.** All caller-facing routes depend on `current_principal`. With `AUTH_ENABLED=false` it returns
  a fixed local principal. With it on, a signed cookie session (OAuth code + PKCE, allow-listed e-mails,
  same-origin check for state-changing requests) is required for `/api/*`.
- **Language.** System prompt and messages are data files; adding a language needs no code change.

## Not in Phase 1 (known gaps)

- **Isolation is for stability, not security.** Each run is a separate worker process, so a hung or crashing run
  is killed/failed without taking the API down, and cancel/timeout are hard stops. But the worker has the same
  secrets and network access as the API process, and document parsing is still not sandboxed. A restricted
  worker container (no secrets beyond what the run needs, resource limits, own network policy) is the next step.
  Worker start-up costs a few seconds (imports); a warm worker pool is a possible optimisation.
- **Shell / Python / browser tools** are intentionally absent; they need a separate sandbox container.
  An `interrupt()`-based approval flow (human-in-the-loop) is not wired up.
- **No UI beyond a developer page.** The planned UI uses `@langchain/langgraph-sdk` + assistant-ui against
  `/api/langgraph/*`, which is why that API keeps the SDK's shape.
- Saved agents, schedules, workflows, per-agent model selection UI.
- **Metrics** record counts, latency and tokens only. There is no price table or spend calculation: the gateway
  (LiteLLM) already tracks spend per key/model, which is the source of truth for cost. Token counts depend on
  what the gateway reports; streamed calls need `stream_options.include_usage` (enabled by default here).
  A gateway that rejects it will fail model calls: adjust `llm/gateway.py` in that case.
- Retention/cleanup of events, checkpoints, inputs and artifacts is a manual operation.
- Postgres roles: the app expects permission to create its schema/tables; a locked-down role split
  (migration role vs runtime role) is not set up.
