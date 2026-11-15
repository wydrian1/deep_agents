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

Shared knowledge-base (memory) and notes tools, visual workflows, the daily summary, shell/Python/browser tools and
the human-approval `interrupt()` flow, a separate parser process for documents (parsing is in-process), the Windows job
object (replaced by process-group kills and container limits), and everything that depended on the dashboard or a
Google/GitHub OAuth app (Gmail, Calendar, private GitHub, push notifications, NAS health).
