# AI Agent Usage Dashboard

A local Docker dashboard for measuring AI-agent workload across the existing Codex telemetry store and Paperclip-managed agents.

The application keeps the detailed Codex analysis that already existed while adding a normalized provider-agnostic layer for agents, providers, models, billing modes, accounts/subscriptions, local models, runtime and cost comparison.

## Architecture

```text
~/.codex rollouts (read-only)             Paperclip HTTP API (read-only)
            |                                       |
            v                                       v
 collect_codex_usage.py                  collect_paperclip_usage.py
            |                                       |
            +-------------------+-------------------+
                                |
                                v
                    normalized usage model
                 usage_records / usage_runs
                                |
                                v
                    data/codex_usage.sqlite
                                |
                                v
                         local Python API
                                |
                                v
                     http://127.0.0.1:8765
```

The retained Codex tables continue powering Codex-specific context, compaction, skills, rate-limit and MCP-tool diagnostics. `usage_model.py` projects Codex into the normalized tables and Paperclip feeds the same model.

Normal analytics do not browse the Paperclip Docker volume or ACP session files. The integration uses Paperclip HTTP APIs and does not persist prompts, tool arguments, credentials, vault data or authentication tokens.

## Normalized usage

`usage_records` stores additive usage observations and `usage_runs` stores run/turn lifecycle data. Important dimensions include:

- source system (`codex_desktop` or `paperclip`)
- run/session identity and timestamps
- agent id/name/role
- provider, runtime/harness and model
- billing mode: `subscription`, `api`, `local` or `unknown`
- immutable account/connection id plus a current human-readable display name
- fresh/cached/cache-write/output/reasoning/total tokens when available
- actual provider charge when the source reports it
- current API-equivalent estimated cost when the model is priced
- task/project ids where available
- duration/status

Runs with unavailable token counters are still retained. The dashboard shows the metrics that exist instead of inventing missing values.

### Cross-source duplicate protection

When a Paperclip run and Codex telemetry expose the same durable provider-session id, default all-source views prefer Paperclip for run/account attribution. Codex response tokens are suppressed only when the matching Paperclip run also exposes token metrics. Explicit Source filters still let you inspect either source independently.

No fuzzy matching is used.

## Paperclip integration

The collector uses read-only GET requests to the supported Paperclip API, including:

- `/api/companies`
- `/api/companies/:companyId/agents`
- `/api/companies/:companyId/ai-connections`
- `/api/companies/:companyId/heartbeat-runs`
- `/api/companies/:companyId/costs/quota-windows`

Paperclip heartbeat-run `usageJson` is the run-level usage source. Aggregate cost APIs are not added back into token totals, avoiding a second copy of the same workload.

### Account/subscription attribution

Account attribution is run-level, not agent-level. The collector reads the run's `contextSnapshot.aiConnection.connectionId` and resolves that immutable id against the AI-connection catalogue.

This allows multiple agents to share an account and lets one agent move from Subscription A to Subscription B later without rewriting history. Account display-name changes also do not split historical identity: the immutable connection id remains the grouping key and the current catalogue label is used for display.

If a connection cannot be resolved, its stable id is retained and the display label is `Unknown account`; the dashboard does not guess.

## Configuration

Paperclip normally needs no extra setup when one company is visible and the local API accepts the container request. The Docker default is:

```env
PAPERCLIP_ENABLED=true
PAPERCLIP_BASE_URL=http://host.docker.internal:3100
PAPERCLIP_COMPANY_ID=
PAPERCLIP_API_TOKEN=
PAPERCLIP_TIMEOUT_SECONDS=10
```

Copy `.env.example` to a local `.env` only if you need overrides.

- Set `PAPERCLIP_COMPANY_ID` when more than one company is visible. The collector refuses to guess.
- Set `PAPERCLIP_API_TOKEN` only if the Paperclip instance requires it. It remains server-side and is never exposed to browser JavaScript.
- Set `PAPERCLIP_ENABLED=false` to disable Paperclip collection while retaining Codex.

## Local / Ollama usage

Paperclip/OpenCode runs routed to Ollama are represented as local billing. For example:

```text
Provider:     Ollama
Billing mode: Local
Account:      Local Ollama
Runtime:      OpenCode
Model:        gpt-oss:20b
```

If token usage is reported, it is captured. If not, the run still contributes its agent/model/timestamps/duration. Local execution is labelled `No provider charge`; this does not claim hardware and electricity have zero economic cost.

## Start the dashboard

Prerequisites:

- Docker Desktop
- this repository cloned locally
- Codex data at `%USERPROFILE%\.codex` on Windows
- Paperclip on port 3100 if Paperclip collection is wanted

```powershell
docker compose up -d --build
```

Open `http://127.0.0.1:8765`.

After pulling changes:

```powershell
git pull
docker compose up -d --build
```

## Refresh behavior

Use **Refresh data** in the dashboard. One refresh workflow now runs the existing Codex collector, projects Codex into the normalized model, runs the read-only Paperclip collector, marks proven cross-source overlaps and clears the dashboard cache.

Supported modes are **Last N days**, **Date range**, and the Codex **Full rollout scan** option for historical/parser backfills.

Selected date partitions are replaced rather than appended, so repeated refreshes are idempotent. If Paperclip is unavailable, previous Paperclip data is retained, its Source status becomes unavailable, and successful Codex data remains usable.

## Filters

The global filter row supports:

- date range and 7 / 30 / 60 / 90 day shortcuts
- source
- billing mode
- provider
- account / subscription
- agent
- model
- reasoning effort
- project
- target model on the Workload estimator tab

Filters compose server-side in SQLite.

## Dashboard views

### AI usage

Unified workload by provider, billing mode, account/subscription, agent, model, source and day. Runs without token counters still contribute to run metrics.

### Subscription value

Separates reported provider charge from the counterfactual API-equivalent estimate. It also shows subscription/API/local/unknown run counts, account/agent/provider/billing breakdowns, agent compute time, active wall-clock time, parallelism, and API-equivalent cost per compute/active hour.

An agent compute-hour is machine/agent processing time, not a claim of equivalent human developer labour.

### Workload estimator

Select an agent, historical period and target model. The estimator shows runs, runs/day, token coverage, fresh/cached/output workload, average/P95/peak measured-run tokens, runtime distribution, current API-equivalent estimate and target-model API-equivalent estimate where a configured price exists.

It is a workload replay, not a model-quality or behaviour forecast. A different model may tokenize, cache, reason, call tools and produce output differently.

### Activity & tools

Provider-agnostic runtime/model activity plus Codex-specific skills and MCP integration/tool calls.

### Codex diagnostics

Retains Codex-specific context, compaction, latency, failure and rate-limit telemetry.

### Runs

Normalized run-level source, agent, account, provider, model, duration, token availability, reported provider charge, API-equivalent estimate and status.

## Pricing and cost semantics

The current OpenAI API rate card remains in `data/api_pricing.json`. **Check latest OpenAI prices** refreshes supported Standard API token prices and reprices historical normalized workload without recollecting sessions.

Unsupported providers/models remain unpriced rather than receiving invented rates.

Reported provider cost and API-equivalent estimated cost are separate values. Local billing has no provider/API charge but is not described as zero total economic cost.

## Subscription quota limitation

Historical token counts are workload measurements; they are **not** a conversion into ChatGPT/Codex subscription allowance.

The current Paperclip quota-window endpoint reports provider-level windows and does not expose the AI connection/account id needed to distinguish separate Codex subscriptions. The dashboard can therefore show provider-level quota windows but deliberately does not attribute one of those windows to an individual subscription.

The normalized quota table already has an account-connection field so account-specific quota can be added later if Paperclip exposes reliable connection attribution.

## Historical Codex telemetry

Newer Codex usage comes from per-response `token_usage_record` events. Older `event_msg -> token_count` telemetry is recovered from `info.last_token_usage`, while cumulative totals are used only as a change detector. This prevents inherited parent history in forked/subagent rollouts from being counted as a huge new request and preserves requests across cumulative-counter resets.

Copied historical turn lifecycles are de-duplicated only when turn id, start, completion and duration prove they are the same lifecycle. Generic old turn ids are not globally collapsed.

## Privacy and security

Docker mounts:

```text
%USERPROFILE%/.codex  -> /codex     read-only
./data                -> /app/data  read/write
```

Paperclip access is HTTP GET/read-only. The integration does not modify agents, tasks, AI connections, subscriptions, runtime configuration or settings.

Never commit `.env`, raw rollout JSONL, SQLite/database files, tokens, auth files or validation packs. The repository safety workflow checks tracked files for telemetry and common credential patterns.

## Local data

SQLite remains the dashboard source of truth at `data/codex_usage.sqlite`. It contains both the retained Codex-specific tables and normalized tables:

```text
usage_records
usage_runs
usage_accounts
source_status
provider_quotas
```

## CLI collectors

Normal use should happen through the dashboard. Troubleshooting examples:

```powershell
py .\collect_codex_usage.py --days 7
py .\collect_paperclip_usage.py --days 7
py .\collect_codex_usage.py --from-date 2026-07-01 --to-date 2026-09-23 --scan-all
```

## Development checks

GitHub Actions verifies:

- Python syntax for both collectors, normalized model and server
- Codex collector CLI/smoke run
- legacy token-count inherited-history/reset behavior
- Codex skill and typed MCP parsing
- Paperclip shared-subscription/account switching/local/API/missing-token/unknown-account/idempotency/outage fixtures
- normalized SQLite dashboard queries and workload estimator
- local API startup and health
- dashboard JavaScript syntax
- Docker image build
- Windows CLI wrapper
- sensitive-data / telemetry repository safety

## Known limitations

- Paperclip's heartbeat-run list currently caps a request at 1,000 runs. The collector queries per agent and surfaces a warning when an agent reaches that limit; pagination support would remove this gap.
- Actual provider cost is available only when the source reports it.
- Non-OpenAI API-equivalent prices are not invented; provider price cards can be added later.
- Current Paperclip quota windows are provider-scoped, not connection/account-scoped.
- Codex-specific skills/MCP/context diagnostics do not automatically exist for other runtimes.
- No electricity/hardware cost is estimated for local inference.
