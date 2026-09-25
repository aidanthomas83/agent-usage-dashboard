#!/usr/bin/env python3
"""Read-only Paperclip usage collector for the normalized AI usage dataset.

The collector intentionally uses Paperclip's supported HTTP API. It does not
browse Paperclip's Docker volume, ACP session files, vault, or credential
storage, and it never persists authentication material.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import collect_codex_usage as codex
import usage_model


DEFAULT_BASE_URL = "http://host.docker.internal:3100"


class PaperclipApiError(RuntimeError):
    pass


class PaperclipClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path: str) -> Any:
        path = path if path.startswith("/api/") else "/api/" + path.lstrip("/")
        headers = {
            "Accept": "application/json",
            "User-Agent": "agent-usage-dashboard/1.0 (read-only Paperclip collector)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            raise PaperclipApiError(
                f"Paperclip HTTP {exc.code} for {path}: {body[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PaperclipApiError(f"Paperclip request failed for {path}: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaperclipApiError(f"Paperclip returned non-JSON data for {path}") from exc


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Collect Paperclip agent usage into the normalized dashboard dataset.")
    p.add_argument("--days", type=int, help="Local calendar days to rebuild, including today.")
    p.add_argument("--from-date", help="First local date to rebuild (YYYY-MM-DD).")
    p.add_argument("--to-date", help="Last local date to rebuild, inclusive (YYYY-MM-DD).")
    p.add_argument("--output-dir", type=Path, default=Path(os.environ.get("DATA_DIR", here / "data")))
    p.add_argument("--base-url", default=os.environ.get("PAPERCLIP_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID", ""))
    p.add_argument("--api-token", default=os.environ.get("PAPERCLIP_API_TOKEN", ""))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("PAPERCLIP_TIMEOUT_SECONDS", "10")))
    return p.parse_args()


def selected_dates(args: argparse.Namespace, local_tz) -> set[str]:
    today = datetime.now(local_tz).date()
    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            raise ValueError("--from-date and --to-date must be provided together.")
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        if end < start:
            raise ValueError("--to-date must be on or after --from-date.")
    else:
        days = 7 if args.days is None else args.days
        if days < 1:
            raise ValueError("--days must be at least 1.")
        end = today
        start = today - timedelta(days=days - 1)
    return {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}


def truthy_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def iso_utc(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else ""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first_value(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


def number(source: dict[str, Any], *names: str) -> int:
    value = first_value(source, *names)
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def number_present(source: dict[str, Any], *names: str) -> bool:
    return any(name in source and source[name] is not None for name in names)


def text(source: dict[str, Any], *names: str) -> str:
    value = first_value(source, *names)
    return str(value).strip() if value is not None else ""


def normalize_runtime(adapter_type: str) -> str:
    key = str(adapter_type or "").strip().lower()
    mapping = {
        "codex_local": "codex",
        "claude_local": "claude",
        "opencode_local": "opencode",
        "paperclip_runner": "paperclip-runner",
    }
    return mapping.get(key, key.replace("_local", "").replace("_", "-") or "unknown")


def split_provider_model(model: str) -> tuple[str, str]:
    value = str(model or "").strip()
    if "/" not in value:
        return "", value
    provider, rest = value.split("/", 1)
    return provider.strip().lower(), rest.strip()


def normalize_billing(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_")
    if key in {"subscription", "included", "plan"}:
        return "subscription"
    if key in {"api", "api_key", "metered", "usage"}:
        return "api"
    if key in {"local", "ollama", "self_hosted", "selfhosted"}:
        return "local"
    return "unknown"


def discover_company(client: PaperclipClient, configured: str) -> tuple[str, str]:
    if configured:
        return configured, "configured"
    payload = client.get("/api/companies")
    companies = payload if isinstance(payload, list) else as_dict(payload).get("companies")
    if not isinstance(companies, list):
        raise PaperclipApiError("Paperclip /api/companies did not return a company list")
    ids = [str(row.get("id") or "") for row in companies if isinstance(row, dict) and row.get("id")]
    if len(ids) == 1:
        return ids[0], "discovered"
    if not ids:
        raise PaperclipApiError("Paperclip has no visible company to collect")
    raise PaperclipApiError(
        "Paperclip exposes multiple companies; set PAPERCLIP_COMPANY_ID so usage is not attributed to the wrong company"
    )


def connection_catalog(payload: Any) -> dict[str, dict[str, Any]]:
    body = as_dict(payload)
    rows = body.get("connections") if isinstance(body.get("connections"), list) else []
    return {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }


def model_provider_from_run(
    run: dict[str, Any],
    agent: dict[str, Any],
    connection: dict[str, Any],
) -> tuple[str, str]:
    usage = as_dict(run.get("usageJson"))
    context = as_dict(run.get("contextSnapshot"))
    ai = as_dict(context.get("aiConnection"))
    adapter = as_dict(agent.get("adapterConfig"))

    model = (
        text(usage, "model", "modelName", "model_name")
        or text(context, "model", "modelName")
        or text(adapter, "model")
    )
    provider = (
        text(usage, "provider", "providerName", "provider_name")
        or text(ai, "provider")
        or text(connection, "provider")
    ).lower()

    prefix, normalized_model = split_provider_model(model)
    if not provider and prefix:
        provider = prefix
    if prefix and prefix == provider:
        model = normalized_model

    runtime = normalize_runtime(str(agent.get("adapterType") or ""))
    if not provider:
        if runtime == "codex":
            provider = "openai"
        elif runtime == "claude":
            provider = "anthropic"

    aliases = {
        "local": "ollama",
        "openai-compatible": "unknown",
    }
    provider = aliases.get(provider, provider) or "unknown"
    return provider, model or "Unknown"


def token_usage(run: dict[str, Any]) -> dict[str, Any]:
    usage = as_dict(run.get("usageJson"))
    input_names = ("inputTokens", "input_tokens", "rawInputTokens", "raw_input_tokens")
    cached_names = (
        "cachedInputTokens", "cached_input_tokens",
        "cacheReadInputTokens", "cache_read_input_tokens",
    )
    output_names = ("outputTokens", "output_tokens", "rawOutputTokens", "raw_output_tokens")
    write_names = ("cacheWriteInputTokens", "cache_write_input_tokens")
    reasoning_names = ("reasoningOutputTokens", "reasoning_output_tokens")

    available = any(
        number_present(usage, *names)
        for names in (input_names, cached_names, output_names, write_names, reasoning_names)
    )
    inp = number(usage, *input_names)
    cached = number(usage, *cached_names)
    cache_write = number(usage, *write_names)
    out = number(usage, *output_names)
    reasoning = number(usage, *reasoning_names)
    # Paperclip's own RunUsage normalization treats inputTokens and
    # cachedInputTokens as separate additive categories.
    total = inp + cached + cache_write + out
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "fresh_input_tokens": inp + cache_write,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "token_metrics_available": 1 if available else 0,
    }


def normalize_run(
    run: dict[str, Any],
    agent: dict[str, Any],
    connections: dict[str, dict[str, Any]],
    local_tz,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = str(run.get("id") or "")
    context = as_dict(run.get("contextSnapshot"))
    ai = as_dict(context.get("aiConnection"))
    connection_id = text(ai, "connectionId", "connection_id")
    connection = connections.get(connection_id, {})
    provider, model = model_provider_from_run(run, agent, connection)
    runtime = normalize_runtime(str(agent.get("adapterType") or ""))

    usage = as_dict(run.get("usageJson"))
    explicit_billing = first_value(usage, "billingType", "billing_type", "billingMode", "billing_mode")
    method = (
        text(ai, "method")
        or text(connection, "method")
        or text(as_dict(agent.get("runtimeConfig")).get("aiConnection") if isinstance(as_dict(agent.get("runtimeConfig")).get("aiConnection"), dict) else {}, "method")
    )
    billing_mode = normalize_billing(explicit_billing or method)
    if provider == "ollama":
        billing_mode = "local"

    if provider == "ollama":
        account_connection_id = connection_id or "local:ollama"
        account_display_name = connection.get("name") or "Local Ollama"
    elif connection_id:
        account_connection_id = connection_id
        account_display_name = (
            str(connection.get("name") or "").strip()
            or str(connection.get("accountLabel") or "").strip()
            or f"{provider.title()} account"
        )
    else:
        account_connection_id = ""
        account_display_name = "Unknown account"

    started = iso_dt(run.get("startedAt")) or iso_dt(run.get("createdAt"))
    finished = iso_dt(run.get("finishedAt"))
    duration_ms = (
        max(0, int((finished - started).total_seconds() * 1000))
        if started and finished and finished >= started
        else 0
    )
    local_date = (started or finished or datetime.now(timezone.utc)).astimezone(local_tz).date().isoformat()

    usage_values = token_usage(run)
    actual_cost_value = first_value(usage, "costCents", "cost_cents", "providerCostCents", "provider_cost_cents")
    actual_available = actual_cost_value is not None
    try:
        actual_cost = float(actual_cost_value or 0) / 100.0
    except (TypeError, ValueError):
        actual_cost = 0.0
        actual_available = False
    if billing_mode == "local":
        # This is specifically provider/API charge, not total economic cost.
        actual_cost = 0.0
        actual_available = True

    estimated_cost: float | None = None
    estimated_status = "unavailable"
    if provider == "openai" and usage_values["token_metrics_available"]:
        priced_model = model.split("/", 1)[-1] if model.startswith("openai/") else model
        source_input = (
            usage_values["input_tokens"]
            + usage_values["cached_input_tokens"]
            + usage_values["cache_write_input_tokens"]
        )
        estimated_cost, estimated_status, _long = codex.estimate_api_cost(
            priced_model,
            source_input,
            usage_values["cached_input_tokens"],
            usage_values["cache_write_input_tokens"],
            usage_values["output_tokens"],
        )
        if not estimated_status.startswith("priced"):
            estimated_cost = None

    session_id = (
        str(run.get("sessionIdAfter") or "").strip()
        or str(run.get("sessionIdBefore") or "").strip()
        or text(context, "sessionId", "session_id")
    )
    task_id = text(context, "issueId", "issue_id", "taskId", "task_id")
    project_id = text(context, "projectId", "project_id")
    project_name = text(context, "projectName", "project_name")

    agent_id = str(run.get("agentId") or agent.get("id") or "")
    agent_name = str(agent.get("name") or agent_id or "Unknown agent")
    agent_role = str(agent.get("role") or agent.get("title") or "")
    run_key = f"{usage_model.SOURCE_PAPERCLIP}:run:{run_id}"
    status = str(run.get("status") or "unknown")
    source_detail = "paperclip_heartbeat_run"
    if billing_mode == "local":
        source_detail += ":no_provider_charge"

    common = {
        "source_system": usage_model.SOURCE_PAPERCLIP,
        "run_key": run_key,
        "run_id": run_id,
        "session_id": session_id,
        "date": local_date,
        "started_utc": iso_utc(started),
        "finished_utc": iso_utc(finished),
        "duration_ms": duration_ms,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_role": agent_role,
        "provider": provider,
        "runtime": runtime,
        "model": model,
        "billing_mode": billing_mode,
        "account_connection_id": account_connection_id,
        "account_display_name": account_display_name,
        **usage_values,
        "actual_provider_cost_usd": actual_cost if actual_available else None,
        "actual_cost_available": 1 if actual_available else 0,
        "estimated_api_cost_usd": estimated_cost,
        "estimated_cost_status": estimated_status,
        "task_id": task_id,
        "project_id": project_id,
        "project_name": project_name,
        "reasoning_effort": text(usage, "reasoningEffort", "reasoning_effort"),
        "service_tier": text(usage, "serviceTier", "service_tier"),
        "status": status,
        "source_detail": source_detail,
        "is_primary": 1,
        "duplicate_of": "",
    }
    record = {
        "record_key": f"{usage_model.SOURCE_PAPERCLIP}:run-usage:{run_id}",
        "record_grain": "run",
        "source_record_id": run_id,
        "timestamp_utc": iso_utc(finished or started),
        **common,
    }
    run_row = dict(common)
    return record, run_row


def write_quotas(
    conn: sqlite3.Connection,
    client: PaperclipClient,
    company_id: str,
) -> tuple[bool, str]:
    observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        payload = client.get(f"/api/companies/{urllib.parse.quote(company_id)}/costs/quota-windows")
    except PaperclipApiError as exc:
        return False, str(exc)
    rows = payload if isinstance(payload, list) else []
    conn.execute(
        "DELETE FROM provider_quotas WHERE source_system=?",
        (usage_model.SOURCE_PAPERCLIP,),
    )
    for result in rows:
        if not isinstance(result, dict):
            continue
        provider = str(result.get("provider") or "unknown")
        for window in result.get("windows") or []:
            if not isinstance(window, dict):
                continue
            label = str(window.get("label") or "Quota")
            key = f"paperclip:{provider}:{label}"
            conn.execute(
                """INSERT OR REPLACE INTO provider_quotas(
                       quota_key,source_system,provider,account_connection_id,label,
                       used_percent,resets_at,value_label,detail,observed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    key, usage_model.SOURCE_PAPERCLIP, provider, "", label,
                    window.get("usedPercent"), window.get("resetsAt"),
                    window.get("valueLabel"), window.get("detail"), observed,
                ),
            )
    return True, "Provider-level quota windows refreshed; Paperclip does not identify a connection/account for these windows."


def collect(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "codex_usage.sqlite"
    if not db_path.exists():
        print("Paperclip collector skipped: analytics SQLite database does not exist yet.", flush=True)
        return 0

    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        usage_model.ensure_schema(conn)
        if not truthy_env("PAPERCLIP_ENABLED", True):
            usage_model.set_source_status(
                conn, usage_model.SOURCE_PAPERCLIP, "disabled",
                "Paperclip collection is disabled by PAPERCLIP_ENABLED.",
            )
            conn.commit()
            print("Paperclip collection disabled.", flush=True)
            return 0
    finally:
        conn.close()

    local_tz = datetime.now().astimezone().tzinfo
    dates = selected_dates(args, local_tz)
    client = PaperclipClient(args.base_url, args.api_token, args.timeout)

    try:
        company_id, company_resolution = discover_company(client, args.company_id)
        agents_payload = client.get(
            f"/api/companies/{urllib.parse.quote(company_id)}/agents"
        )
        agents = agents_payload if isinstance(agents_payload, list) else []
        agent_map = {
            str(agent.get("id")): agent
            for agent in agents
            if isinstance(agent, dict) and agent.get("id")
        }

        connection_error = ""
        try:
            connections = connection_catalog(
                client.get(f"/api/companies/{urllib.parse.quote(company_id)}/ai-connections")
            )
        except PaperclipApiError as exc:
            connections = {}
            connection_error = str(exc)

        runs_by_id: dict[str, dict[str, Any]] = {}
        truncated_agents: list[str] = []
        for agent_id in agent_map:
            path = (
                f"/api/companies/{urllib.parse.quote(company_id)}/heartbeat-runs"
                f"?agentId={urllib.parse.quote(agent_id)}&limit=1000"
            )
            payload = client.get(path)
            rows = payload if isinstance(payload, list) else []
            if len(rows) >= 1000:
                truncated_agents.append(agent_id)
            for run in rows:
                if isinstance(run, dict) and run.get("id"):
                    runs_by_id[str(run["id"])] = run

        records: list[dict[str, Any]] = []
        run_rows: list[dict[str, Any]] = []
        accounts_seen: dict[str, tuple[str, str, str]] = {}
        for run in runs_by_id.values():
            agent = agent_map.get(str(run.get("agentId") or ""), {})
            record, run_row = normalize_run(run, agent, connections, local_tz)
            if record["date"] not in dates:
                continue
            records.append(record)
            run_rows.append(run_row)
            connection_id = str(record["account_connection_id"] or "")
            if connection_id:
                accounts_seen[connection_id] = (
                    str(record["account_display_name"] or connection_id),
                    str(record["provider"] or "unknown"),
                    str(record["billing_mode"] or "unknown"),
                )

        conn = sqlite3.connect(db_path, timeout=60)
        try:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            usage_model.ensure_schema(conn)
            record_count, run_count = usage_model.replace_source_dates(
                conn, usage_model.SOURCE_PAPERCLIP, dates, records, run_rows
            )

            # Keep the stable connection catalogue independent of agent/model
            # changes. Connection names may change without breaking history.
            for connection_id, connection in connections.items():
                provider = str(connection.get("provider") or "unknown")
                billing = normalize_billing(connection.get("method"))
                display = (
                    str(connection.get("name") or "").strip()
                    or str(connection.get("accountLabel") or "").strip()
                    or f"{provider.title()} account"
                )
                usage_model.upsert_account(
                    conn, usage_model.SOURCE_PAPERCLIP, connection_id, display,
                    provider, billing, str(connection.get("status") or "unknown"),
                )
            for connection_id, (display, provider, billing) in accounts_seen.items():
                if connection_id not in connections:
                    usage_model.upsert_account(
                        conn, usage_model.SOURCE_PAPERCLIP, connection_id, display,
                        provider, billing, "observed",
                    )

            quota_ok, quota_note = write_quotas(conn, client, company_id)
            usage_model.mark_cross_source_duplicates(conn)

            details = {
                "base_url": args.base_url,
                "company_resolution": company_resolution,
                "agents_seen": len(agent_map),
                "connections_resolved": len(connections),
                "connection_catalog_error": connection_error or None,
                "run_limit_reached_for_agents": len(truncated_agents),
                "quota_scope": "provider_only" if quota_ok else "unavailable",
                "quota_note": quota_note,
                "selected_from": min(dates),
                "selected_to": max(dates),
            }
            message = (
                f"Collected {run_count:,} Paperclip runs from {len(agent_map):,} agents."
                + (" Some account names could not be resolved." if connection_error else "")
                + (" One or more agents reached the 1,000-run API limit." if truncated_agents else "")
            )
            usage_model.set_source_status(
                conn, usage_model.SOURCE_PAPERCLIP, "ok", message,
                company_id=company_id, records_upserted=record_count,
                details_json=json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                success=True,
            )
            conn.commit()
        finally:
            conn.close()

        print(
            f"Paperclip refresh complete: {record_count:,} usage record(s), "
            f"{run_count:,} run(s), {len(connections):,} connection(s).",
            flush=True,
        )
        if connection_error:
            print("Paperclip account catalogue unavailable; unresolved runs are labelled Unknown account.", flush=True)
        if truncated_agents:
            print(
                f"Paperclip warning: {len(truncated_agents)} agent(s) reached the current 1,000-run list limit.",
                flush=True,
            )
        return 0

    except (PaperclipApiError, ValueError) as exc:
        # Paperclip is optional to dashboard availability. Persist a safe status
        # and return success so a Codex refresh is still usable.
        conn = sqlite3.connect(db_path, timeout=60)
        try:
            usage_model.ensure_schema(conn)
            usage_model.set_source_status(
                conn, usage_model.SOURCE_PAPERCLIP, "unavailable", str(exc),
                company_id=args.company_id,
                details_json=json.dumps(
                    {"base_url": args.base_url, "selected_from": min(dates), "selected_to": max(dates)},
                    separators=(",", ":"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        print(f"Paperclip unavailable: {exc}", flush=True)
        return 0


def main() -> int:
    args = parse_args()
    try:
        return collect(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
