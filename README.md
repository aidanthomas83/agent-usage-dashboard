# Agent Usage Dashboard

A local, Dockerized dashboard for analysing Codex usage from the rollout telemetry stored under `~/.codex`.

The application is designed to answer questions such as:

- Which models and agents are consuming the most capacity?
- How much input is fresh vs cached?
- How much would the selected token usage cost through the OpenAI API at the embedded current rate card?
- Which sessions, agents and reasoning levels are most expensive?
- How much compaction, tool and skill activity is occurring?
- What value am I getting from my Codex / ChatGPT subscription?

## Architecture

```text
~/.codex (read-only)
      |
      v
collector
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

The browser no longer loads a generated `codex_usage_data.js` file. Dashboard queries are executed against SQLite and returned asynchronously as small aggregated JSON responses.

The detailed CSV files are still produced as local exports/debugging data, but they are not used by the dashboard runtime.

## Start the dashboard

Prerequisites:

- Docker Desktop
- this repository cloned locally
- Codex data at `%USERPROFILE%\.codex` on Windows

From the repository folder, build and start it once:

```powershell
docker compose up -d --build
```

Then open:

```text
http://127.0.0.1:8765
```

The container uses `restart: unless-stopped`, so after the first setup Docker Desktop can restart it automatically when Docker starts.

To stop it:

```powershell
docker compose stop
```

To start it again:

```powershell
docker compose start
```

To rebuild after pulling source changes:

```powershell
git pull
docker compose up -d --build
```

## Refresh data from the dashboard

Use **Refresh data** in the top-right of the dashboard.

Two refresh modes are supported:

- **Last N days** — e.g. 7, 30, 60 or 90 days.
- **Date range** — explicitly select the first and last dates to rebuild.

Refreshes run in the background. The existing dashboard remains usable while the collector is working, and the page automatically reloads the SQLite-backed measurements after the refresh completes.

There is also a **Full rollout scan** option. Use this for historical backfills or after changes to the rollout parser. Normal incremental refreshes do not usually need it.

Refreshes remain idempotent: the selected local calendar dates are rebuilt rather than appended, so rerunning the same range does not duplicate usage.

## Docker mounts and privacy

The Compose configuration mounts:

```text
%USERPROFILE%/.codex  ->  /codex     read-only
./data                ->  /app/data  read/write
```

The Codex source mount is deliberately **read-only**. The collector cannot modify the local Codex sessions or agent definitions.

The web service is exposed only on:

```text
127.0.0.1:8765
```

rather than all LAN interfaces.

The repository does not source-control local telemetry. `.gitignore`, `.dockerignore` and the GitHub Actions safety job exclude or check for:

- `data/`
- Codex JSONL/session data
- SQLite/database files
- environment files
- API keys / common credentials
- private keys
- user-specific local paths

## Local data

Runtime data is written beneath the gitignored `data/` folder:

```text
data/
  codex_usage.sqlite
  codex_usage_records.csv
  codex_turns.csv
  codex_activity.csv
  codex_rate_limits.csv
  codex_usage_daily.csv
  codex_agents.csv
  codex_usage_metadata.json
```

SQLite is the dashboard source of truth. CSVs are retained for inspection/export.

## Dashboard filtering

The dashboard supports:

- date range
- 7 / 30 / 60 / 90-day shortcuts
- model
- agent / role
- reasoning effort
- project

Filters are sent to the local API and applied in SQLite. The browser does not need to download all raw response records to change a filter.

## API-equivalent token cost

The dashboard calculates a **counterfactual Standard OpenAI API token cost in USD** for the selected usage.

It uses the token categories persisted by Codex:

```text
ordinary input
cached input
cache-write input
output
```

and the embedded current API rate card, including documented long-context multipliers where applicable.

This is intended as a subscription-value comparison rather than an invoice. It does not include separately priced API services such as web search, containers, storage, regional processing or other non-token charges.

The dashboard also keeps the separate Codex/Business credit-equivalent estimate.

## Historical Codex telemetry

The collector supports both:

- newer per-response `token_usage_record` events
- older cumulative `event_msg -> token_count -> info.total_token_usage` events

For older rollouts, request usage is recovered from positive changes in cumulative totals. This avoids double-counting repeated `last_token_usage` snapshots.

The terminal/debug metadata distinguishes the requested refresh window from dates where recoverable token data was actually observed.

## CLI collector

The collector can still be run directly for troubleshooting or exports, although normal use should now happen through the dashboard.

Examples:

```powershell
py .\collect_codex_usage.py --days 7

py .\collect_codex_usage.py --from-date 2026-07-01 --to-date 2026-09-23 --scan-all
```

The legacy `run-usage-report.cmd` wrapper is retained for troubleshooting compatibility.

## Development checks

GitHub Actions verifies:

- Python syntax
- collector CLI startup
- collector smoke run
- legacy `token_count` telemetry regression
- local API server startup and health endpoint
- dashboard JavaScript syntax
- Docker image build
- Windows CLI wrapper
- sensitive-data / telemetry repository safety
