# Codex Usage Dashboard

A local, read-only Codex telemetry collector plus an offline dashboard.

## Quick start on Windows

1. Extract/keep this folder anywhere convenient.
2. Make sure Python 3.10+ is installed (`py --version`).
3. Run:

   ```bat
   run-usage-report.cmd 7
   ```

`7` means **today plus the previous 6 local calendar days**. The command refreshes those day partitions and opens `dashboard\index.html`.

Direct usage:

```powershell
py .\collect_codex_usage.py --days 30
```

Alternative Codex home:

```powershell
py .\collect_codex_usage.py --days 30 --codex-home "D:\path\to\.codex"
```

Force a complete rollout scan when file mtimes are unreliable:

```powershell
py .\collect_codex_usage.py --days 30 --scan-all
```

## Idempotent updates

The collector does **not append blindly**. For every date included in `--days N`, it removes existing rows for that local calendar date, rebuilds them from rollout files, deduplicates them, and atomically rewrites the datasets. Rerunning the same period therefore updates/corrects the same date partitions rather than creating duplicates.

## Generated data

- `data/codex_usage_records.csv` — one row per unique model response/token record.
- `data/codex_turns.csv` — task/turn lifecycle: model, effort, duration, TTFT, status, context window, token totals.
- `data/codex_activity.csv` — tool, plugin and detected skill activity. Raw tool arguments/output are **not** stored.
- `data/codex_rate_limits.csv` — observed primary/secondary rate-limit pressure where Codex persisted it.
- `data/codex_usage_daily.csv` — daily aggregate.
- `data/codex_agents.csv` — custom agent definitions under `%USERPROFILE%\.codex\agents`.
- `data/codex_usage_data.js` — local browser bundle used by the dashboard.
- `data/codex_usage_metadata.json` — collector metadata and credit-rate assumptions.
- `dashboard/index.html` — fully local dashboard; no npm install or web server required.

## Token accounting

The collector uses request-level `token_usage_record` entries, deduplicated by `(thread_id, response_id)`.

It records:

- model and reasoning effort actually persisted for the turn;
- service tier where available (`default`, `priority` / Fast, etc.);
- main thread vs subagent and stable agent role;
- input, cached input, fresh input, cache-write input, output and reasoning-output tokens;
- compaction responses;
- thread/session lifecycle timestamps.

`fresh_input_tokens = input_tokens - cached_input_tokens`.

Reasoning output is already included in output tokens, so it is **not added a second time**.

## Estimated Codex credits

The collector calculates an **estimated token-based credit equivalent** from fresh input, cached input and output tokens using the current ChatGPT Business / Codex rate card embedded in the script.

As of **23 Sep 2026**, the principal rates embedded are:

| Model | Input / 1M | Cached / 1M | Output / 1M |
|---|---:|---:|---:|
| GPT-6 Astra | 250 credits | 25 | 1,250 |
| GPT-5.6 Sol | 100 credits | 10 | 500 |
| GPT-5.6 Terra | 50 credits | 5 | 300 |
| GPT-5.6 Luna | 5 credits | 0.5 | 30 |

The Sol rate reflects the current promotional purchased-credit rate. Additional published models are also included in the collector. `codex-auto-review` is priced using GPT-5.4 because the current OpenAI rate card states Auto review uses GPT-5.4.

The dashboard shows **credit coverage**. Models or Fast-tier combinations without a current published mapping are excluded rather than guessed. Codex does not charge for cache writes. These values are estimates from local rollout telemetry, not an authoritative invoice or workspace billing ledger.

Current rate-card reference: https://help.openai.com/en/articles/11481834-cha

## New dashboard views

### Consistent colour system

Every model receives one stable colour across:

- Daily token mix
- Models
- Tokens by model
- Estimated credits by day/model
- Turns by model
- Highest-usage session model pills

Every agent/role also receives one stable colour across agent charts and credit/response breakdowns.

### Daily token mix

- Stacked by model.
- Date axis is `DD-MON` while chronological sorting remains ISO-date based.
- Hovering a day shows cached input, fresh input, output, total tokens and the per-model split.

### Agents / roles

- Aggregates by stable role (`executor`, `guardian_review`, etc.), not Codex-generated nicknames.
- Scans `.codex\agents\*.toml` and shows configured agents as status pills.
- Status is represented by the pill dot only: used in the selected range, used elsewhere in collected history, or never observed.

### Estimated credits

Includes estimated credits by:

- day;
- model;
- agent/role;
- session (in the sortable session table).

### Useful insights

Where the rollout data supports them, the dashboard includes:

- average and P95 input tokens per model response;
- context amplification (`total input processed / fresh input`);
- compaction token and estimated-credit overhead;
- average/P95 turn duration;
- average/P95 time to first token;
- failed/aborted turns;
- P95 context-window utilisation;
- tool/plugin activity;
- best-effort detected skill usage from injected `<skill>` fragments, `skills://` reads, and local `SKILL.md` reads;
- response density and agent efficiency;
- observed primary/secondary rate-limit pressure.

### Product/activity charts

Inspired by Codex/OpenAI's own analytics views:

- Tokens by model over time;
- Turns by model over time;
- Patch lines changed per day;
- Tool/plugin activity over time;
- Skills used over time.

`Patch lines changed` is an approximation based on additions + deletions in successful/observed `apply_patch` diffs. It is not a final Git repository diff and should not be treated as an exact lines-of-code productivity measure.

Skill/plugin detection depends on what the Codex client persisted into the rollout. The collector now detects structured injected `<skill><name>...</name>...</skill>` fragments, `skills://.../skill.md` reads, local `.../skills/<name>/SKILL.md` reads, and recognisable `mcp__...` calls. Codex does not currently persist a first-class local skill invocation event in the ordinary rollout/log stream, so this remains best-effort and can still be incomplete.

### Highest-usage sessions

All columns are sortable. The table includes:

- session start and latest activity;
- top-model colour pill;
- agents, turns and responses;
- compactions;
- estimated credits + credit coverage;
- fresh/cached/output/total tokens;
- accumulated turn duration;
- maximum observed context utilisation;
- failed/aborted turn count.

## Privacy

Everything stays local. The collector reads your local Codex files and writes summary datasets beside the dashboard. It deliberately does **not** write prompt text, assistant messages, tool arguments, tool output, or source-code contents into the analytics datasets.

## Source control safety

This repository is designed to contain **source code only**. Generated Codex telemetry can include session names, project paths, agent names, timestamps, identifiers, and usage history, so it should stay local.

The included `.gitignore` excludes generated `data/`, preview/test outputs, Codex JSONL/session databases, environment files, keys, backups, and other local artefacts. A GitHub Actions safety check also rejects tracked telemetry paths and scans tracked text files for common credential formats and user-specific absolute home paths.

Before publishing a change manually, you can run:

```powershell
py .\scripts\check_repo_safety.py
```

Do not force-add ignored telemetry files with `git add -f`.