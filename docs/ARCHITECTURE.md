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
| `saved/` | Saved agents: `schedule.py` (pure next-occurrence rules, DST-safe), `store.py` (CRUD, optimistic revisions, atomic claim + start + advance with `SKIP LOCKED`), `limits.py` (rate-limit pause), `scheduler.py` (ticker thread; every replica may run one). |
| `memory/store.py`, `tools/memory.py`, `api/memory.py` | Shared knowledge base: versioned entries with exact-revision writes, logical delete, receipts for idempotency, hard limits; the two tool grants; the owner API. |
| `workflows/` | `spec.py` (validated DAG), `store.py` (definitions, immutable versions with frozen agent snapshots, runs, schedules, lease recovery), `engine.py` (`Controller`: an outer LangGraph `StateGraph` on PostgresSaver that reserves ordinary agent runs and waits with `interrupt()`; `ControllerPool` threads in the API process), `scheduler.py`, `diagnostics.py`. `api/workflows.py` exposes it. |
| `runs/outcome.py` | Failed-tool-result counting (four stop a run) and the business outcome status. |
| `runs/metrics.py` | LangChain callback writing one content-free `llm_calls` row per model call (model, status, duration, tokens). Never alters or fails a run. |
| `api/routes.py` | `/api/runs`, `/api/files`, `/api/tools`, artifact download, `/api/runs/{id}/metrics`, `/api/metrics/summary`. |
| `api/langgraph.py` | Narrow LangGraph-SDK-compatible read API (thread state, SSE join, cancel). |
| `auth/` | `current_principal` seam; optional OAuth 2.0 + PKCE login (off by default). |
| `tools/` | Registry and tools. Web access only through `tools/web.py`. |
| `api/graph_view.py` | Studio graph projection: the real topology plus normalized, bounded step events after a cursor. |
| `ui/` (Vite + React) | The web UI. Built into `static/app` and served by `main.py` at `/` with a strict CSP; talks to the API only through `ui/src/api.ts`; strings in `ui/src/i18n`. |
| `summary.py`, `api/summary.py` | The daily summary (`GET /api/summary`): content-free counts, causes and token totals for the current day in `SCHEDULE_TIMEZONE`. |
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

Differences from the earlier dashboard-embedded implementation, and why, are recorded in
[DIVERGENCES.md](DIVERGENCES.md); keep it current when you change a deliberate difference.

## Workflow engine in one paragraph

A workflow run row holds the frozen spec and agents. A controller (thread in the API process) takes a 120 s
lease, heartbeats, and drives a LangGraph graph with a Postgres checkpoint. An agent node
reserves a normal run through `RunStore.reserve_in` (inheriting the workflow's deadline, unique per run and node)
and pauses with an interrupt (`workflow_child`, `workflow_capacity`, `workflow_approval`); the controller resumes it
when the child finishes. If the controller dies, the scheduler tick of any replica relaunches it once the lease has expired (`WORKFLOW_AUTO_RESUME`, on by default; the owner's `continue` does the same on demand) and it resumes from the checkpoint; a step
that crashed while launching is marked "uncertain" and never relaunched automatically.

## Isolation

Every run is a separate worker process, so a hung or crashing run is killed or failed without taking the API down
and cancel/timeout are hard stops. On top of that the worker gets only what a run needs:

| Layer | What it does |
| --- | --- |
| Environment | An allow-list of process variables (`PATH`, locale, CA bundle, ...) plus the settings a run needs (database, gateway, limits). The login secrets (`OAUTH_*`, `SESSION_*`, `COOKIE_*`), the scheduler switch, tracing, provider and proxy variables never reach it; `AUTH_ENABLED` is forced off in the worker. |
| Files | A private scratch directory `DATA_DIR/work/<run id>` is the worker's cwd and `TMPDIR`; it is removed when the run ends. |
| Resources (POSIX) | `WORKER_MEMORY_MB` (address space, off by default), `WORKER_FILE_MB` (largest file, 512), `WORKER_OPEN_FILES` (1024), no core dumps, `umask 077`, `PR_SET_NO_NEW_PRIVS`. Applied in the child between fork and exec, with everything looked up beforehand so the child never takes a lock another thread holds. |
| Container | `docker-compose.yml`: read-only root file system (only `/data` and `/tmp`), all capabilities dropped, `no-new-privileges`, memory/CPU/pids limits, a non-root user. |

What is **not** isolated: the worker still holds the database and gateway credentials (it writes events and calls
the model), and it shares the container's network. Web tools are SSRF-guarded in the application, but that is not
a network boundary. For one, run the service on a network whose only routes out are the LiteLLM gateway, the
database and an egress proxy, and give the worker its own database role with access to the app schema only.
Shell, Python and browser tools would additionally need their own sandbox container; they are not part of this
service.

## Not in Phase 1 (known gaps)

- **Isolation is hardening, not a sandbox** (see the next section). Document parsing is still in the worker
  process, and the network is the container's. Worker start-up costs a few seconds (imports); a warm worker pool
  is a possible optimisation.
- **Shell / Python / browser tools** are intentionally absent; they need a separate sandbox container, and a
  human-approval flow for shell commands (workflows already have approval steps).
- **UI scope.** The Studio covers what the service supports. It uses `@langchain/langgraph-sdk` + assistant-ui
  against `/api/langgraph/*`, which is why that API keeps the SDK's shape.
- A per-agent model selection UI.
- **Metrics** record counts, latency and tokens only. There is no price table or spend calculation: the gateway
  (LiteLLM) already tracks spend per key/model, which is the source of truth for cost. Token counts depend on
  what the gateway reports; streamed calls need `stream_options.include_usage` (enabled by default here).
  A gateway that rejects it will fail model calls: adjust `llm/gateway.py` in that case.
- Retention/cleanup of events, checkpoints, inputs and artifacts is a manual operation.
- Postgres roles: the app expects permission to create its schema/tables; a locked-down role split
  (migration role vs runtime role) is not set up.
