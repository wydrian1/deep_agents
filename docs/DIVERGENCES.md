# Deliberate divergences from the previous implementation

This service was derived from an earlier, dashboard-embedded Deep Agents implementation. The rule while porting:
**keep the old behaviour unless the new design is clearly better; where it is, diverge on purpose and record
it here.** This file is that record. If you change one of these decisions, update this file in the same PR.

## Where the new design is intentionally different (and why)

| Area | Previous | Now | Why the new one is better |
| --- | --- | --- | --- |
| Runtime | Node dashboard spawning Python per request, Windows | One FastAPI service in a Linux container, external Postgres | Standalone, several replicas possible, no per-request process bridge |
| Model access | ChatGPT OAuth token store, fixed model | LiteLLM gateway (`LLM_GATEWAY_URL`/`LLM_API_KEY`), model = gateway alias, resolved when a run **starts** | Any provider behind one key; a changed `DEFAULT_MODEL` applies to saved agents |
| Provider request options | Always `parallel_tool_calls=false` and `prompt_cache_key` | Both opt-in (`LLM_PARAMS_JSON`, `LLM_PROMPT_CACHE_KEY`) | Not every provider behind a gateway accepts them; a rejected parameter would fail every call |
| Schedule time zone | Fixed `Europe/Budapest` | `SCHEDULE_TIMEZONE` (default UTC) | Not tied to one deployment |
| Hourly schedule | Assumes whole-hour UTC offsets | Computes the UTC minute from the zone's current offset | Identical result for whole-hour zones; also correct for half/quarter-hour zones |
| Schedule model | `frequency: none` + derived `enabled` | `schedule: null` plus an explicit `enabled` flag; schedule can be saved while paused | Pausing keeps the rule; resuming needs consent again |
| Scheduler concurrency | Single SQLite writer | `FOR UPDATE SKIP LOCKED` claims and a Postgres advisory lock for run slots | Several replicas never start one occurrence twice |
| Rate-limit pause | Fixed 2 h estimate | `RATE_LIMIT_COOLDOWN_SECONDS` (default 1 h, 0 = off) | A gateway 429 is often a per-minute limit; tunable per deployment |
| Error text | Localized text stored in the DB | A message **key** stored, localized when read (`APP_LANGUAGE`, per-run `language`) | Language is configurable without rewriting history; no raw error text can leak |
| Error classification | By status/exception name | Same, but also walks wrapped causes and `response.status_code` | Wrapped client errors (e.g. connection errors) were being reported as generic failures |
| Tool-call cap | 200 calls per run for document/search/report only | 200 calls per run across **all** tools | A runaway web-fetch loop costs as much as a runaway report loop |
| Tool audit | Only the document/search/report tools | Every tool call, written as `started` first | Uniform accounting; an interrupted call never counts as success (kept from the old guard) |
| Tool names | `nas_file`, `nas_search`, `report_create(format, title, ...)` | `files`, `file_search`, `report_create(title, kind, ...)` (+ `txt`) | No NAS in the name; argument order reads naturally |
| Document paging | Character `offset`/`limit` | 32,000-character pages (`page`) | Simpler for the model; same bounds |
| Duplicate tools/files in a request | Rejected | De-duplicated | More forgiving, no information lost |
| Saved-agent files | Per-agent immutable snapshot copies | Shared uploaded files by id, existence + SHA-256 checked at start and at every read | No duplicated blobs; tampering still detected |
| Extra metrics endpoint | Per-run only | Also `GET /api/metrics/summary` (per model) | Cheap and useful with a multi-model gateway |
| Token usage | First generation only | Sums all generations of a call | Correct when a call returns several generations |
| Stored runs | Hard cap of 1000 runs | No cap (Postgres); retention is a manual operation | The cap only protected SQLite; a hard cap would silently stop scheduled agents |
| Execution mode | Always a supervisor + worker | `RUN_MODE=process` (default) or `thread` (tests/debug) | Fast tests; same code path in `execute_run` |
| Auth | Dashboard session | `current_principal` seam; optional OAuth 2.0 + PKCE login, **off** by default | Standalone; login can be enabled later without touching endpoints |
| Run prompt limit | 4,000 characters | 4,000 by default, `MAX_PROMPT_CHARS` to change | Same default, tunable |

## Web UI: taken over, with these deliberate differences

The Studio design, class names, CSS (dark and light), graph/inspector/event views, run statistics, agent library,
weekly calendar and composer dialogs come from the earlier implementation. Differences:

| Area | Previous | Now | Why |
| --- | --- | --- | --- |
| Framework | Next.js page inside the dashboard shell | Vite + React single-page app served by the API (`ui/`) | Standalone; no dashboard, no server-side rendering needed |
| Language | Hungarian only | English and Hungarian (`ui/src/i18n`), default from `APP_LANGUAGE`, switchable in the header | Configurable language is a requirement of the standalone service |
| Theme | Followed the dashboard theme | Own dark/light toggle (system preference by default) | No dashboard to inherit from |
| Live thread | Loaded with the page | Loaded on demand (code split) | Main bundle 330 kB instead of 1.3 MB |
| Time zone | Always Europe/Budapest | The service's `SCHEDULE_TIMEZONE` | Same reason as the scheduler |
| Overview card | "Awaiting approval" | "Need attention" (failed or blocked recent runs) | Approvals exist only inside workflows and are handled in the Workflows view |
| File picking | NAS Files module, folder snapshots | Upload + choose among uploaded files | No NAS Files module here |
| Tool names/descriptions | Tool catalog JSON | UI dictionaries (fallback: backend catalog text) | Localized |
| Outcome sentence | "Business outcome: Business outcome not verified" | "Business outcome: not verified" | The old sentence repeated itself |
| Report export | "Save to Files" action | Download only | There is no shared Files store to export into |
| Account | Dashboard session | Login button / account name / log out when `AUTH_ENABLED=true` | The OAuth seam |

Not in the UI because the service does not have the feature yet: shell-command approval, and the
Gmail/Calendar/private-GitHub/ChatGPT-login panels.

## Knowledge base and workflows: what differs from the previous implementation

Rules ported unchanged: names/limits (200 names, 2,000 versions, 4 MiB, 12,000 characters), exact-revision writes,
logical delete with history, restore = new revision, no inherited grants (an arrow grants nothing), write requires
read; workflow limits (2-12 nodes, 8 agents, 24 edges, 4 in / 3 out), fixed text conditions only, immutable versions
with frozen agents, agent-edit propagation in the same transaction, at most three active workflows, consents for data
flow and recurring runs, schedules tied to one version, no automatic retry, "uncertain" launch after a crash.

| Area | Previous | Now | Why |
| --- | --- | --- | --- |
| Workflow controller | Separate supervised process per workflow, SQLite checkpoint file | Thread in the API process, LangGraph on `PostgresSaver`, 120 s lease + heartbeat, owner-triggered `continue` | Standalone service, no extra process bridge; the lease and checkpoint keep a single controller per run |
| Waiting | Polling retry loop with files | `interrupt()` (`workflow_child`, `workflow_capacity`, `workflow_approval`) resumed by the controller; poll interval `WORKFLOW_POLL_SECONDS` | The outer graph never blocks a thread inside a node |
| Consent flags | Separate model/shell/data-flow consents | Data-flow consent (start, schedule) + recurring consent | There are no shell/model-switch tools here yet |
| Memory write idempotency | Tool-call id | A digest of the call's own content (+ actor) | Replays after a checkpoint resume return the first result without relying on provider call ids |
| Workflow errors | Localized text in the DB | Message keys (`key` or `key:cause`), localized when read | Same reason as for run errors |
| Diagnostics | SQLite table, owner endpoint | Sanitized `workflow_diagnostics` table (<= 200, one per run), no HTTP endpoint | Content-free operator data stays in the database |
| Knowledge API | Tool-style single endpoint | REST (`GET/PUT /api/memory/{key}`, `/history`, `/delete`), 409 carries `current_revision` | Plain HTTP semantics; same checks |
| Outcome profiles | Parcel/Gmail-specific outcome rules | Generic business outcome status only | Those integrations do not exist here |
| Controller recovery | Owner pressed "continue" after a lease expired | The scheduler tick relaunches expired controllers itself (`WORKFLOW_AUTO_RESUME`, default on; a never-claimed run after 60 s); `continue` still works | An API restart no longer leaves a workflow idle until someone notices; a live lease is never taken over and finished steps are never relaunched |
| Diagnostics | No HTTP endpoint | `GET /api/workflow-diagnostics` (owner API, content-free bodies) | The reports hold only error classes and code locations, so they are safe to read in the UI/CLI |

## Daily summary, worker isolation: what differs

| Area | Previous | Now | Why |
| --- | --- | --- | --- |
| Summary day | Europe/Budapest calendar day | Calendar day in `SCHEDULE_TIMEZONE` | Same reason as the scheduler |
| Summary problems | Failed or blocked/error workflows | The same, but a workflow the owner **cancelled** is not a problem | `cancel` stamps the run with outcome `error`, which made every deliberate cancel look like a failure |
| Summary shape | camelCase, epoch seconds | snake_case, ISO timestamps; the workflow's stored error key is localized when the agent gave no reason | Consistent with the rest of the API |
| Worker environment | The parent's environment minus tracing/provider/proxy variables | An allow-list plus the settings a run needs; no `OAUTH_*`/`SESSION_*`/`COOKIE_*`, `AUTH_ENABLED` and the scheduler off | A worker never serves a request, so it should not hold the login secrets |
| Worker files | cwd = `DATA_DIR` | cwd and `TMPDIR` = a private `DATA_DIR/work/<run id>`, removed afterwards | Nothing a run writes is shared with another run |
| Worker limits | Windows job object (memory, process count) | POSIX rlimits (address space, file size, descriptors), no core dumps, `umask 077`, no-new-privileges, plus the container limits in compose | The service runs in a Linux container; the job object is Windows only |

## Ported unchanged (do not "improve" without a reason)

Concurrency 3 and 3,600 s run limit; no automatic retry at run/tool/scheduler level and at most one model-request
retry; event store bounds (24 MB / 20,000 events / 1 MB each); stale-run recovery (timeout + 60 s, never resubmitted);
schedule rules (daily/weekly on the local calendar, spring-gap times move forward, repeated autumn times run once,
hourly on real 3,600 s boundaries); 5-minute grace, overlap and full-slot deferral then "missed", never replayed;
claim + create run + advance in one transaction; explicit consent for recurring runs; optimistic revisions; 100 saved
agents; manual run records `last_run_id` and clears the last error; start-time check that attached files still exist;
rerun as a new run and "save run as agent" from original settings; rate-limit pause for scheduled starts; stop after
four failed tool results; business outcome status (`success|noop|blocked|error|unknown`); worker environment stripped
of tracing, provider and proxy variables; SSRF rules (HTTPS/443 only, pinned and re-validated addresses, 3 redirects,
2 MB, 20 s overall; the address filter is the stricter of the standard one and the old block list); report and upload
limits; content-free metrics; scheduler heartbeat.

## Not ported yet

Shell/Python/browser tools and shell-command approval, a separate parser process for documents (parsing is in-process), the Windows job
object (replaced by process-group kills and container limits), and everything that depended on the dashboard or a
Google/GitHub OAuth app (Gmail, Calendar, private GitHub, push notifications, NAS health).
