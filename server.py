#!/usr/bin/env python3
"""Local HTTP/API server for the Dockerized Codex usage dashboard."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
import urllib.request
import uuid

import collect_codex_usage as collector
import usage_model
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
LONG_CONTEXT_THRESHOLD = 272_000

_REFRESH_LOCK = threading.Lock()
_REFRESH: dict[str, object] = {
    "id": None,
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "request": None,
    "message": "",
    "phase": "idle",
    "log": [],
    "return_code": None,
}
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, dict[str, object]] = {}
_CACHE_MAX = 128

DEFAULT_PRICING: dict[str, object] = {
    "version": 1,
    "source_url": PRICING_SOURCE_URL,
    "as_of": "2026-09-23",
    "retrieved_at": None,
    "status": "embedded",
    "long_context_threshold": LONG_CONTEXT_THRESHOLD,
    "aliases": {
        "gpt-5.6": "gpt-5.6-sol",
        "daybreak-blue": "gpt-5.6-sol",
        "gpt-daybreak-blue-latest": "gpt-5.6-sol",
        "daybreak-red": "gpt-5.6-cyber",
        "gpt-daybreak-red-latest": "gpt-5.6-cyber",
        "codex-auto-review": "gpt-5.4",
    },
    "models": {
        "gpt-6-astra": {"input": 10.0, "cached_input": 1.0, "cache_write": 12.5, "output": 50.0, "long_input": 20.0, "long_cached_input": 2.0, "long_cache_write": 25.0, "long_output": 75.0},
        "gpt-6-sol": {"input": 2.0, "cached_input": 0.2, "cache_write": 2.5, "output": 10.0, "long_input": 4.0, "long_cached_input": 0.4, "long_cache_write": 5.0, "long_output": 15.0},
        "gpt-6-luna": {"input": 0.1, "cached_input": 0.01, "cache_write": 0.125, "output": 0.5, "long_input": 0.2, "long_cached_input": 0.02, "long_cache_write": 0.25, "long_output": 0.75},
        "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "cache_write": 5.0, "output": 20.0, "long_input": 8.0, "long_cached_input": 0.8, "long_cache_write": 10.0, "long_output": 30.0},
        "gpt-5.6-terra": {"input": 2.0, "cached_input": 0.2, "cache_write": 2.5, "output": 12.0, "long_input": 4.0, "long_cached_input": 0.4, "long_cache_write": 5.0, "long_output": 18.0},
        "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "cache_write": 0.25, "output": 1.2, "long_input": 0.4, "long_cached_input": 0.04, "long_cache_write": 0.5, "long_output": 1.8},
        "gpt-5.6-cyber": {"input": 12.5, "cached_input": 1.25, "cache_write": 15.625, "output": 75.0},
        "gpt-5.5": {"input": 5.0, "cached_input": 0.5, "cache_write": 5.0, "output": 30.0, "long_input": 10.0, "long_cached_input": 1.0, "long_cache_write": 10.0, "long_output": 45.0},
        "gpt-5.4": {"input": 2.5, "cached_input": 0.25, "cache_write": 2.5, "output": 15.0, "long_input": 5.0, "long_cached_input": 0.5, "long_cache_write": 5.0, "long_output": 22.5},
        "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "cache_write": 0.75, "output": 4.5},
        "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "cache_write": 1.75, "output": 14.0},
        "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "cache_write": 1.75, "output": 14.0},
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8765")))
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", ROOT / "data")))
    p.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    return p.parse_args()


def db_path(data_dir: Path) -> Path:
    return data_dir / "codex_usage.sqlite"


def pricing_path(data_dir: Path) -> Path:
    return data_dir / "api_pricing.json"


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def open_db(data_dir: Path) -> sqlite3.Connection | None:
    path = db_path(data_dir)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    try:
        conn.execute("PRAGMA mmap_size=268435456")
    except sqlite3.Error:
        pass
    return conn


def decode_metadata(conn: sqlite3.Connection) -> dict[str, object]:
    try:
        rows = conn.execute("SELECT key,value FROM metadata").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, object] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except Exception:
            out[row["key"]] = row["value"]
    return out


def agent_expr(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (
        f"CASE WHEN {p}agent_type='Main' THEN 'Main' "
        f"ELSE COALESCE(NULLIF({p}agent_role,''),NULLIF({p}agent_label,''),'Subagent') END"
    )


def parse_filters(query: dict[str, list[str]]) -> dict[str, str]:
    def one(name: str) -> str:
        return (query.get(name) or [""])[0].strip()

    result = {
        "from": one("from"),
        "to": one("to"),
        "source": one("source"),
        "billing": one("billing"),
        "provider": one("provider"),
        "account": one("account"),
        "model": one("model"),
        "agent": one("agent"),
        "effort": one("effort"),
        "project": one("project"),
        "target_model": one("target_model"),
    }
    for key in ("from", "to"):
        if result[key]:
            date.fromisoformat(result[key])
    return result


def where_for(filters: dict[str, str], alias: str = "") -> tuple[str, list[object]]:
    """Filters for the retained Codex-specific tables.

    Cross-provider filters deliberately collapse this query to no rows when the
    selection cannot represent Codex Desktop. This prevents a Paperclip/Ollama
    filter from silently showing unrelated Codex-only diagnostics.
    """
    p = f"{alias}." if alias else ""
    clauses: list[str] = []
    values: list[object] = []
    if filters.get("source") and filters["source"] != usage_model.SOURCE_CODEX:
        clauses.append("1=0")
    if filters.get("billing") and filters["billing"] != "subscription":
        clauses.append("1=0")
    if filters.get("provider") and filters["provider"] != "openai":
        clauses.append("1=0")
    if filters.get("account") and filters["account"] != "codex-desktop":
        clauses.append("1=0")
    if filters.get("from"):
        clauses.append(f"{p}date>=?")
        values.append(filters["from"])
    if filters.get("to"):
        clauses.append(f"{p}date<=?")
        values.append(filters["to"])
    if filters.get("model"):
        clauses.append(f"{p}model=?")
        values.append(filters["model"])
    if filters.get("agent"):
        agent = filters["agent"]
        if agent == "codex:main":
            role = "Main"
        elif agent.startswith("codex:role:"):
            role = agent.split(":", 2)[2]
        else:
            clauses.append("1=0")
            role = ""
        if role:
            clauses.append(f"{agent_expr(alias)}=?")
            values.append(role)
    if filters.get("effort"):
        clauses.append(f"{p}reasoning_effort=?")
        values.append(filters["effort"])
    if filters.get("project"):
        clauses.append(f"{p}project=?")
        values.append(filters["project"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


def normalized_where(
    filters: dict[str, str],
    alias: str = "",
    *,
    include_target: bool = False,
) -> tuple[str, list[object]]:
    """Composable filters for usage_records/usage_runs.

    Default all-source queries use only primary rows so an exact Paperclip ↔
    Codex session overlap is not double-counted. Selecting an explicit source
    exposes that source's own raw normalized view, including overlap rows.
    """
    p = f"{alias}." if alias else ""
    clauses: list[str] = []
    values: list[object] = []
    if not filters.get("source"):
        clauses.append(f"{p}is_primary=1")
    if filters.get("from"):
        clauses.append(f"{p}date>=?")
        values.append(filters["from"])
    if filters.get("to"):
        clauses.append(f"{p}date<=?")
        values.append(filters["to"])
    mapping = {
        "source": "source_system",
        "billing": "billing_mode",
        "provider": "provider",
        "account": "account_connection_id",
        "model": "model",
        "agent": "agent_id",
        "effort": "reasoning_effort",
        "project": "project_name",
    }
    for key, column in mapping.items():
        if filters.get(key):
            clauses.append(f"{p}{column}=?")
            values.append(filters[key])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


def rows_as_dicts(rows) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def source_label(value: str) -> str:
    return {
        usage_model.SOURCE_CODEX: "Codex Desktop",
        usage_model.SOURCE_PAPERCLIP: "Paperclip",
    }.get(str(value or ""), str(value or "Unknown").replace("_", " ").title())


def normalized_group_rows(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    key_expr: str,
    name_expr: str | None = None,
) -> list[dict[str, object]]:
    record_where, record_values = normalized_where(filters, "r")
    run_where, run_values = normalized_where(filters, "u")
    name_sql = name_expr or key_expr
    tokens = rows_as_dicts(conn.execute(
        f"""SELECT {key_expr.replace('{a}', 'r')} id,
                   {name_sql.replace('{a}', 'r')} name,
                   COALESCE(SUM(r.total_tokens),0) total_tokens,
                   COALESCE(SUM(r.fresh_input_tokens),0) fresh_input_tokens,
                   COALESCE(SUM(r.cached_input_tokens),0) cached_input_tokens,
                   COALESCE(SUM(r.output_tokens),0) output_tokens
              FROM usage_records r{record_where}
             GROUP BY id,name""",
        record_values,
    ).fetchall())
    runs = rows_as_dicts(conn.execute(
        f"""SELECT {key_expr.replace('{a}', 'u')} id,
                   {name_sql.replace('{a}', 'u')} name,
                   COUNT(*) runs,
                   SUM(CASE WHEN u.token_metrics_available=1 THEN 1 ELSE 0 END) token_runs,
                   COALESCE(SUM(u.duration_ms),0) duration_ms
              FROM usage_runs u{run_where}
             GROUP BY id,name""",
        run_values,
    ).fetchall())
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for row in tokens + runs:
        key = (str(row.get("id") or ""), str(row.get("name") or "Unknown"))
        bucket = merged.setdefault(key, {
            "id": key[0], "name": key[1], "total_tokens": 0,
            "fresh_input_tokens": 0, "cached_input_tokens": 0,
            "output_tokens": 0, "runs": 0, "token_runs": 0, "duration_ms": 0,
        })
        for field in (
            "total_tokens", "fresh_input_tokens", "cached_input_tokens",
            "output_tokens", "runs", "token_runs", "duration_ms",
        ):
            bucket[field] = int(bucket.get(field) or 0) + int(row.get(field) or 0)
    return sorted(
        merged.values(),
        key=lambda row: (int(row.get("total_tokens") or 0), int(row.get("runs") or 0)),
        reverse=True,
    )


def normalized_pricing_groups(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    dimensions: list[tuple[str, str]],
    pricing: dict[str, object],
) -> list[dict[str, object]]:
    where, values = normalized_where(filters, "r")
    where += (" AND " if where else " WHERE ") + "r.token_metrics_available=1"
    threshold = int(pricing.get("long_context_threshold") or LONG_CONTEXT_THRESHOLD)
    dim_select = ", ".join(f"{expr} AS {name}" for name, expr in dimensions)
    dim_group = ", ".join(name for name, _expr in dimensions)
    prefix = (dim_select + ", ") if dim_select else ""
    group = (dim_group + ", ") if dim_group else ""
    rows = conn.execute(
        f"""SELECT {prefix}
                   COALESCE(NULLIF(r.model,''),'Unknown') model,
                   CASE WHEN
                     (r.input_tokens+r.cached_input_tokens+r.cache_write_input_tokens)>{threshold}
                   THEN 1 ELSE 0 END long_context,
                   SUM(r.input_tokens) input_tokens,
                   SUM(r.cached_input_tokens) cached_input_tokens,
                   SUM(r.cache_write_input_tokens) cache_write_input_tokens,
                   SUM(r.output_tokens) output_tokens,
                   SUM(r.total_tokens) total_tokens
              FROM usage_records r{where}
             GROUP BY {group}model,long_context""",
        values,
    ).fetchall()
    return price_group_rows(rows, [name for name, _expr in dimensions], pricing)


def merged_normalized_interval_ms(rows) -> int:
    converted = [
        {"started_utc": row.get("started_utc"), "completed_utc": row.get("finished_utc")}
        for row in rows
    ]
    return merged_interval_ms(converted)


def normalized_runtime_breakdown(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    pricing: dict[str, object],
) -> dict[str, object]:
    where, values = normalized_where(filters, "u")
    rows = rows_as_dicts(conn.execute(
        f"""SELECT u.* FROM usage_runs u{where} ORDER BY u.date,u.started_utc""",
        values,
    ).fetchall())
    compute_ms = sum(int(row.get("duration_ms") or 0) for row in rows)
    active_ms = merged_normalized_interval_ms(rows)

    costs = normalized_pricing_groups(conn, filters, [], pricing)
    estimated = sum(float(row.get("api_cost") or 0) for row in costs)
    actual_where, actual_values = normalized_where(filters, "u")
    actual = conn.execute(
        f"""SELECT COALESCE(SUM(actual_provider_cost_usd),0),
                   SUM(CASE WHEN actual_cost_available=1 THEN 1 ELSE 0 END),
                   COUNT(*)
              FROM usage_runs u{actual_where}""",
        actual_values,
    ).fetchone()

    by_agent: dict[str, dict[str, object]] = {}
    for row in rows:
        agent_id = str(row.get("agent_id") or "")
        bucket = by_agent.setdefault(agent_id, {
            "id": agent_id,
            "name": str(row.get("agent_name") or "Unknown agent"),
            "agent_compute_ms": 0,
            "turns": 0,
            "_intervals": [],
        })
        bucket["agent_compute_ms"] = int(bucket["agent_compute_ms"]) + int(row.get("duration_ms") or 0)
        bucket["turns"] = int(bucket["turns"]) + 1
        bucket["_intervals"].append(row)

    cost_by_agent = {
        str(row.get("id") or ""): float(row.get("api_cost") or 0)
        for row in normalized_pricing_groups(
            conn, filters, [("id", "r.agent_id")], pricing
        )
    }
    agents: list[dict[str, object]] = []
    for agent_id, bucket in by_agent.items():
        active = merged_normalized_interval_ms(bucket.pop("_intervals"))
        compute = int(bucket["agent_compute_ms"])
        cost = cost_by_agent.get(agent_id, 0.0)
        bucket["active_wall_ms"] = active
        bucket["parallelism_factor"] = (compute / active) if active else 0.0
        bucket["estimated_api_cost"] = cost
        bucket["cost_per_agent_hour"] = cost / (compute / 3_600_000.0) if compute else 0.0
        bucket["cost_per_active_hour"] = cost / (active / 3_600_000.0) if active else 0.0
        agents.append(bucket)
    agents.sort(key=lambda row: int(row.get("agent_compute_ms") or 0), reverse=True)

    return {
        "summary": {
            "agent_compute_ms": compute_ms,
            "active_wall_ms": active_ms,
            "parallelism_factor": (compute_ms / active_ms) if active_ms else 0.0,
            "estimated_api_cost": estimated,
            "actual_provider_cost": float(actual[0] or 0),
            "actual_cost_runs": int(actual[1] or 0),
            "runs": int(actual[2] or 0),
            "cost_per_agent_hour": estimated / (compute_ms / 3_600_000.0) if compute_ms else 0.0,
            "cost_per_active_hour": estimated / (active_ms / 3_600_000.0) if active_ms else 0.0,
        },
        "agents": agents,
    }


def parse_iso_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def merged_interval_ms(rows) -> int:
    """Return union duration for timestamp pairs, so overlapping agents count once."""
    intervals: list[tuple[datetime, datetime]] = []
    for row in rows:
        start = parse_iso_timestamp(row["started_utc"])
        end = parse_iso_timestamp(row["completed_utc"])
        if start is None or end is None or end <= start:
            continue
        intervals.append((start, end))
    if not intervals:
        return 0
    intervals.sort(key=lambda pair: pair[0])
    total = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            if end > current_end:
                current_end = end
            continue
        total += (current_end - current_start).total_seconds() * 1000.0
        current_start, current_end = start, end
    total += (current_end - current_start).total_seconds() * 1000.0
    return int(round(total))


def runtime_breakdown(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    pricing: dict[str, object],
) -> dict[str, object]:
    """Compute additive agent time, merged wall time, and API-equivalent hourly cost."""
    turn_where, turn_values = where_for(filters)
    by_agent_daily = rows_as_dicts(conn.execute(
        f"""SELECT date,{agent_expr()} name,
                   COUNT(*) turns,
                   COALESCE(SUM(duration_ms),0) duration_ms,
                   SUM(CASE WHEN duration_ms>0 THEN 1 ELSE 0 END) duration_covered_turns,
                   SUM(CASE WHEN started_utc<>'' AND completed_utc<>'' THEN 1 ELSE 0 END) interval_covered_turns
              FROM turns{turn_where}
             GROUP BY date,{agent_expr()}
             ORDER BY date,duration_ms DESC""",
        turn_values,
    ).fetchall())
    turn_intervals = rows_as_dicts(conn.execute(
        f"""SELECT date,{agent_expr()} name,started_utc,completed_utc,duration_ms
              FROM turns{turn_where}
             ORDER BY date,started_utc""",
        turn_values,
    ).fetchall())

    intervals_by_date: dict[str, list[dict[str, object]]] = {}
    intervals_by_agent: dict[str, list[dict[str, object]]] = {}
    for row in turn_intervals:
        day = str(row.get("date") or "")
        name = str(row.get("name") or "Unknown")
        if day:
            intervals_by_date.setdefault(day, []).append(row)
        intervals_by_agent.setdefault(name, []).append(row)
    active_by_date = {day: merged_interval_ms(rows) for day, rows in intervals_by_date.items()}
    active_by_agent = {name: merged_interval_ms(rows) for name, rows in intervals_by_agent.items()}

    daily_map: dict[str, dict[str, object]] = {}
    agent_map: dict[str, dict[str, object]] = {}
    for row in by_agent_daily:
        day = str(row.get("date") or "")
        name = str(row.get("name") or "Unknown")
        daily = daily_map.setdefault(day, {
            "date": day, "agent_compute_ms": 0, "active_wall_ms": active_by_date.get(day, 0),
            "turns": 0, "duration_covered_turns": 0, "interval_covered_turns": 0, "api_cost": 0.0,
        })
        daily["agent_compute_ms"] = int(daily["agent_compute_ms"]) + int(row.get("duration_ms") or 0)
        daily["turns"] = int(daily["turns"]) + int(row.get("turns") or 0)
        daily["duration_covered_turns"] = int(daily["duration_covered_turns"]) + int(row.get("duration_covered_turns") or 0)
        daily["interval_covered_turns"] = int(daily["interval_covered_turns"]) + int(row.get("interval_covered_turns") or 0)

        agent = agent_map.setdefault(name, {
            "name": name, "agent_compute_ms": 0, "active_wall_ms": active_by_agent.get(name, 0),
            "turns": 0, "duration_covered_turns": 0, "interval_covered_turns": 0,
            "api_cost": 0.0, "total_tokens": 0, "priced_tokens": 0,
        })
        agent["agent_compute_ms"] = int(agent["agent_compute_ms"]) + int(row.get("duration_ms") or 0)
        agent["turns"] = int(agent["turns"]) + int(row.get("turns") or 0)
        agent["duration_covered_turns"] = int(agent["duration_covered_turns"]) + int(row.get("duration_covered_turns") or 0)
        agent["interval_covered_turns"] = int(agent["interval_covered_turns"]) + int(row.get("interval_covered_turns") or 0)

    daily_cost_rows = price_group_rows(
        pricing_groups(conn, filters, [("date", "date")], pricing),
        ["date"], pricing,
    )
    for row in daily_cost_rows:
        day = str(row.get("date") or "")
        daily = daily_map.setdefault(day, {
            "date": day, "agent_compute_ms": 0, "active_wall_ms": active_by_date.get(day, 0),
            "turns": 0, "duration_covered_turns": 0, "interval_covered_turns": 0, "api_cost": 0.0,
        })
        daily["api_cost"] = float(row.get("api_cost") or 0.0)

    agent_cost_rows = price_group_rows(
        pricing_groups(conn, filters, [("name", agent_expr())], pricing),
        ["name"], pricing,
    )
    for row in agent_cost_rows:
        name = str(row.get("name") or "Unknown")
        agent = agent_map.setdefault(name, {
            "name": name, "agent_compute_ms": 0, "active_wall_ms": active_by_agent.get(name, 0),
            "turns": 0, "duration_covered_turns": 0, "interval_covered_turns": 0,
            "api_cost": 0.0, "total_tokens": 0, "priced_tokens": 0,
        })
        agent["api_cost"] = float(row.get("api_cost") or 0.0)
        agent["total_tokens"] = int(row.get("total_tokens") or 0)
        agent["priced_tokens"] = int(row.get("priced_tokens") or 0)

    daily_rows: list[dict[str, object]] = []
    for day in sorted(daily_map):
        row = daily_map[day]
        compute_hours = float(row["agent_compute_ms"] or 0) / 3_600_000.0
        active_hours = float(row["active_wall_ms"] or 0) / 3_600_000.0
        cost = float(row["api_cost"] or 0.0)
        row["cost_per_agent_hour"] = cost / compute_hours if compute_hours else 0.0
        row["cost_per_active_hour"] = cost / active_hours if active_hours else 0.0
        row["parallelism_factor"] = compute_hours / active_hours if active_hours else 0.0
        daily_rows.append(row)

    agent_rows: list[dict[str, object]] = []
    for name, row in agent_map.items():
        compute_hours = float(row["agent_compute_ms"] or 0) / 3_600_000.0
        active_hours = float(row["active_wall_ms"] or 0) / 3_600_000.0
        cost = float(row["api_cost"] or 0.0)
        row["cost_per_agent_hour"] = cost / compute_hours if compute_hours else 0.0
        row["cost_per_active_hour"] = cost / active_hours if active_hours else 0.0
        row["parallelism_factor"] = compute_hours / active_hours if active_hours else 0.0
        agent_rows.append(row)
    agent_rows.sort(key=lambda row: int(row.get("agent_compute_ms") or 0), reverse=True)

    total_compute_ms = sum(int(row.get("agent_compute_ms") or 0) for row in daily_rows)
    total_active_ms = sum(int(row.get("active_wall_ms") or 0) for row in daily_rows)
    total_turns = sum(int(row.get("turns") or 0) for row in daily_rows)
    total_duration_covered = sum(int(row.get("duration_covered_turns") or 0) for row in daily_rows)
    total_interval_covered = sum(int(row.get("interval_covered_turns") or 0) for row in daily_rows)
    total_api_cost = sum(float(row.get("api_cost") or 0.0) for row in daily_rows)
    compute_hours = total_compute_ms / 3_600_000.0
    active_hours = total_active_ms / 3_600_000.0
    summary = {
        "agent_compute_ms": total_compute_ms,
        "active_wall_ms": total_active_ms,
        "parallelism_factor": compute_hours / active_hours if active_hours else 0.0,
        "api_cost": total_api_cost,
        "cost_per_agent_hour": total_api_cost / compute_hours if compute_hours else 0.0,
        "cost_per_active_hour": total_api_cost / active_hours if active_hours else 0.0,
        "turns": total_turns,
        "duration_coverage_pct": total_duration_covered / total_turns * 100.0 if total_turns else 0.0,
        "interval_coverage_pct": total_interval_covered / total_turns * 100.0 if total_turns else 0.0,
    }
    return {"summary": summary, "daily": daily_rows, "by_agent_daily": by_agent_daily, "agents": agent_rows}


def percentile(conn: sqlite3.Connection, table: str, field: str, where: str, values: list[object], p: float) -> float:
    condition = f'"{field}">0'
    count_sql = f'SELECT COUNT(*) FROM "{table}"{where} {"AND" if where else "WHERE"} {condition}'
    count = int(conn.execute(count_sql, values).fetchone()[0])
    if count <= 0:
        return 0.0
    offset = round((count - 1) * p)
    sql = f'SELECT "{field}" FROM "{table}"{where} {"AND" if where else "WHERE"} {condition} ORDER BY "{field}" LIMIT 1 OFFSET ?'
    row = conn.execute(sql, [*values, offset]).fetchone()
    return float(row[0] or 0) if row else 0.0


def cached_payload(data_dir: Path, name: str, filters: dict[str, str], extra: tuple, builder):
    key = (
        file_signature(db_path(data_dir)),
        file_signature(pricing_path(data_dir)),
        name,
        tuple(sorted(filters.items())),
        extra,
    )
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    payload = builder()
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = payload
    return payload


class PricingTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def money(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    return float(match.group(1)) if match else None


def load_pricing(data_dir: Path) -> dict[str, object]:
    path = pricing_path(data_dir)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("models"), dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(DEFAULT_PRICING))


def write_pricing(data_dir: Path, payload: dict[str, object]) -> None:
    path = pricing_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    clear_cache()


def fetch_latest_pricing(data_dir: Path) -> dict[str, object]:
    req = urllib.request.Request(
        PRICING_SOURCE_URL,
        headers={
            "User-Agent": "agent-usage-dashboard/1.0 (+local pricing refresh)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = PricingTableParser()
    parser.feed(html)
    current = load_pricing(data_dir)
    models = json.loads(json.dumps(current.get("models") or DEFAULT_PRICING["models"]))
    seen: set[str] = set()
    updated: list[str] = []

    for row in parser.rows:
        if len(row) < 4:
            continue
        model = row[0].strip().lower()
        if not model.startswith("gpt-") or model in seen:
            continue
        values = [money(cell) for cell in row[1:]]
        nums = [v for v in values if v is not None]
        if len(nums) >= 8:
            rates = {
                "input": nums[0], "cached_input": nums[1], "cache_write": nums[2], "output": nums[3],
                "long_input": nums[4], "long_cached_input": nums[5], "long_cache_write": nums[6], "long_output": nums[7],
            }
        elif len(nums) >= 4:
            rates = {"input": nums[0], "cached_input": nums[1], "cache_write": nums[2], "output": nums[3]}
        elif len(nums) == 3:
            rates = {"input": nums[0], "cached_input": nums[1], "cache_write": nums[0], "output": nums[2]}
        else:
            continue
        models[model] = rates
        seen.add(model)
        updated.append(model)

    if not updated:
        raise RuntimeError("OpenAI pricing page was reachable, but no model pricing rows could be parsed.")

    payload = {
        "version": 1,
        "source_url": PRICING_SOURCE_URL,
        "as_of": date.today().isoformat(),
        "retrieved_at": utc_now(),
        "status": "fetched",
        "long_context_threshold": LONG_CONTEXT_THRESHOLD,
        "aliases": current.get("aliases") or DEFAULT_PRICING["aliases"],
        "models": models,
        "updated_models": sorted(updated),
    }
    write_pricing(data_dir, payload)
    return payload


def canonical_model(model: str, pricing: dict[str, object]) -> str:
    key = str(model or "").strip().lower()
    aliases = pricing.get("aliases") or {}
    return str(aliases.get(key, key)) if isinstance(aliases, dict) else key


def rates_for(model: str, pricing: dict[str, object]) -> dict[str, float] | None:
    models = pricing.get("models") or {}
    if not isinstance(models, dict):
        return None
    rates = models.get(canonical_model(model, pricing))
    return rates if isinstance(rates, dict) else None


def api_cost(
    model: str,
    input_tokens: float,
    cached_tokens: float,
    cache_write_tokens: float,
    output_tokens: float,
    long_context: bool,
    pricing: dict[str, object],
) -> tuple[float, bool]:
    rates = rates_for(model, pricing)
    if not rates:
        return 0.0, False
    prefix = "long_" if long_context and "long_input" in rates else ""
    inp_rate = float(rates.get(prefix + "input", rates.get("input", 0)) or 0)
    cached_rate = float(rates.get(prefix + "cached_input", rates.get("cached_input", 0)) or 0)
    write_rate = float(rates.get(prefix + "cache_write", rates.get("cache_write", inp_rate)) or 0)
    out_rate = float(rates.get(prefix + "output", rates.get("output", 0)) or 0)
    ordinary = max(0.0, float(input_tokens) - float(cached_tokens) - float(cache_write_tokens))
    cost = (
        ordinary * inp_rate
        + float(cached_tokens) * cached_rate
        + float(cache_write_tokens) * write_rate
        + float(output_tokens) * out_rate
    ) / 1_000_000.0
    return cost, True


def price_group_rows(rows, dimensions: list[str], pricing: dict[str, object]) -> list[dict[str, object]]:
    grouped: dict[tuple, dict[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        key = tuple(row.get(dim) for dim in dimensions)
        bucket = grouped.setdefault(key, {dim: row.get(dim) for dim in dimensions})
        bucket.setdefault("api_cost", 0.0)
        bucket.setdefault("total_tokens", 0)
        bucket.setdefault("priced_tokens", 0)
        cost, priced = api_cost(
            str(row.get("model") or ""),
            float(row.get("input_tokens") or 0),
            float(row.get("cached_input_tokens") or 0),
            float(row.get("cache_write_input_tokens") or 0),
            float(row.get("output_tokens") or 0),
            bool(row.get("long_context")),
            pricing,
        )
        bucket["api_cost"] = float(bucket["api_cost"]) + cost
        bucket["total_tokens"] = int(bucket["total_tokens"]) + int(row.get("total_tokens") or 0)
        if priced:
            bucket["priced_tokens"] = int(bucket["priced_tokens"]) + int(row.get("total_tokens") or 0)
    return list(grouped.values())


def pricing_groups(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    dimensions: list[tuple[str, str]],
    pricing: dict[str, object],
    extra_clause: str = "",
    extra_values: list[object] | None = None,
):
    where, values = where_for(filters)
    if extra_clause:
        where += (" AND " if where else " WHERE ") + extra_clause
        values.extend(extra_values or [])
    threshold = int(pricing.get("long_context_threshold") or LONG_CONTEXT_THRESHOLD)
    dim_select = ", ".join(f"{expr} AS {alias}" for alias, expr in dimensions)
    dim_group = ", ".join(alias for alias, _ in dimensions)
    select_prefix = (dim_select + ", ") if dim_select else ""
    group_prefix = (dim_group + ", ") if dim_group else ""
    sql = f"""
        SELECT {select_prefix}
               COALESCE(NULLIF(model,''),'Unknown') AS model,
               CASE WHEN input_tokens>{threshold} THEN 1 ELSE 0 END AS long_context,
               SUM(input_tokens) AS input_tokens,
               SUM(cached_input_tokens) AS cached_input_tokens,
               SUM(cache_write_input_tokens) AS cache_write_input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(total_tokens) AS total_tokens
          FROM responses{where}
         GROUP BY {group_prefix}model,long_context
    """
    return conn.execute(sql, values).fetchall()


def meta_payload(data_dir: Path) -> dict[str, object]:
    conn = open_db(data_dir)
    if conn is None:
        return {
            "ready": False, "data_min": None, "data_max": None,
            "models": [], "agents": [], "efforts": [], "projects": [],
            "metadata": {}, "refresh": refresh_status(),
        }
    try:
        metadata = decode_metadata(conn)
        minmax = conn.execute("SELECT MIN(date),MAX(date),COUNT(*) FROM responses").fetchone()

        def distinct(sql: str) -> list[str]:
            return [str(r[0]) for r in conn.execute(sql).fetchall() if r[0] not in (None, "")]

        return {
            "ready": bool(minmax and minmax[2]),
            "data_min": minmax[0] if minmax else None,
            "data_max": minmax[1] if minmax else None,
            "responses": int(minmax[2] or 0) if minmax else 0,
            "models": distinct("SELECT DISTINCT model FROM responses WHERE model<>'' ORDER BY model"),
            "agents": distinct(f"SELECT DISTINCT {agent_expr()} AS role FROM responses ORDER BY role"),
            "efforts": distinct("SELECT DISTINCT reasoning_effort FROM responses WHERE reasoning_effort<>'' ORDER BY reasoning_effort"),
            "projects": distinct("SELECT DISTINCT project FROM responses WHERE project<>'' ORDER BY project"),
            "metadata": metadata,
            "refresh": refresh_status(),
        }
    finally:
        conn.close()


def token_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    def build():
        conn = open_db(data_dir)
        if conn is None:
            return {"ready": False, "tab": "token"}
        try:
            where, values = where_for(filters)
            summary = dict(conn.execute(
                f"""SELECT COUNT(*) responses,
                           COUNT(DISTINCT CASE WHEN session_id<>'' THEN session_id END) sessions,
                           COUNT(DISTINCT CASE WHEN thread_id<>'' THEN thread_id END) threads,
                           COALESCE(SUM(input_tokens),0) input_tokens,
                           COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                           COALESCE(SUM(fresh_input_tokens),0) fresh_input_tokens,
                           COALESCE(SUM(output_tokens),0) output_tokens,
                           COALESCE(SUM(reasoning_output_tokens),0) reasoning_output_tokens,
                           COALESCE(SUM(total_tokens),0) total_tokens,
                           COALESCE(SUM(CASE WHEN agent_type='Subagent' THEN total_tokens ELSE 0 END),0) subagent_tokens,
                           SUM(CASE WHEN is_compaction='true' THEN 1 ELSE 0 END) compactions
                      FROM responses{where}""",
                values,
            ).fetchone())
            total = float(summary["total_tokens"] or 0)
            inp = float(summary["input_tokens"] or 0)
            summary["cache_hit_pct"] = float(summary["cached_input_tokens"] or 0) / inp * 100.0 if inp else 0.0
            summary["subagent_share_pct"] = float(summary["subagent_tokens"] or 0) / total * 100.0 if total else 0.0

            def grouped(expr: str):
                return rows_as_dicts(conn.execute(
                    f"""SELECT {expr} AS name,COUNT(*) responses,
                               SUM(total_tokens) total_tokens,
                               SUM(fresh_input_tokens) fresh_input_tokens,
                               SUM(cached_input_tokens) cached_input_tokens,
                               SUM(output_tokens) output_tokens
                          FROM responses{where}
                         GROUP BY {expr}
                         ORDER BY total_tokens DESC""",
                    values,
                ).fetchall())

            models = grouped("COALESCE(NULLIF(model,''),'Unknown')")
            agents = grouped(agent_expr())
            efforts = grouped("COALESCE(NULLIF(reasoning_effort,''),'Unknown')")
            agent_types = grouped("COALESCE(NULLIF(agent_type,''),'Unknown')")
            daily_models = rows_as_dicts(conn.execute(
                f"""SELECT date,COALESCE(NULLIF(model,''),'Unknown') model,
                           COUNT(*) responses,SUM(total_tokens) total_tokens,
                           SUM(cached_input_tokens) cached_input_tokens,
                           SUM(fresh_input_tokens) fresh_input_tokens,
                           SUM(output_tokens) output_tokens
                      FROM responses{where}
                     GROUP BY date,model
                     ORDER BY date,model""",
                values,
            ).fetchall())

            selected_roles = {str(r["name"]).lower() for r in agents}
            historical_roles = {
                str(r[0]).lower()
                for r in conn.execute(f"SELECT DISTINCT {agent_expr()} FROM responses").fetchall()
                if r[0]
            }
            configured = []
            for row in conn.execute("SELECT name FROM configured_agents ORDER BY name").fetchall():
                name = str(row[0] or "")
                key = name.lower()
                configured.append({
                    "name": name,
                    "status": "used_selected" if key in selected_roles else ("used_historical" if key in historical_roles else "never_seen"),
                })

            return {
                "ready": True, "tab": "token", "filters": filters,
                "metadata": decode_metadata(conn), "summary": summary,
                "models": models, "agents": agents, "efforts": efforts,
                "agent_types": agent_types, "daily_models": daily_models,
                "configured_agents": configured,
            }
        finally:
            conn.close()

    return cached_payload(data_dir, "token", filters, (), build)


def subscription_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    pricing = load_pricing(data_dir)

    def build():
        conn = open_db(data_dir)
        if conn is None:
            return {"ready": False, "tab": "subscription"}
        try:
            total_groups = price_group_rows(pricing_groups(conn, filters, [], pricing), [], pricing)
            total_row = total_groups[0] if total_groups else {"api_cost": 0.0, "total_tokens": 0, "priced_tokens": 0}
            total_tokens = int(total_row.get("total_tokens") or 0)
            summary = {
                "api_cost": float(total_row.get("api_cost") or 0),
                "total_tokens": total_tokens,
                "api_priced_tokens": int(total_row.get("priced_tokens") or 0),
                "api_coverage_pct": (int(total_row.get("priced_tokens") or 0) / total_tokens * 100.0) if total_tokens else 0.0,
            }
            where, values = where_for(filters)
            credit_row = conn.execute(
                f"""SELECT COALESCE(SUM(estimated_credits),0) estimated_credits,
                           COALESCE(SUM(CASE WHEN credit_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END),0) credit_priced_tokens
                      FROM responses{where}""",
                values,
            ).fetchone()
            summary["estimated_credits"] = float(credit_row["estimated_credits"] or 0)
            summary["credit_priced_tokens"] = int(credit_row["credit_priced_tokens"] or 0)
            summary["credit_coverage_pct"] = (summary["credit_priced_tokens"] / total_tokens * 100.0) if total_tokens else 0.0
            long_count = conn.execute(
                f"SELECT COUNT(*) FROM responses{where} {'AND' if where else 'WHERE'} input_tokens>?",
                [*values, int(pricing.get("long_context_threshold") or LONG_CONTEXT_THRESHOLD)],
            ).fetchone()[0]
            summary["long_context_responses"] = int(long_count or 0)

            model_rows = price_group_rows(
                pricing_groups(conn, filters, [("name", "COALESCE(NULLIF(model,''),'Unknown')")], pricing),
                ["name"], pricing,
            )
            agent_rows = price_group_rows(
                pricing_groups(conn, filters, [("name", agent_expr())], pricing),
                ["name"], pricing,
            )
            daily_rows = price_group_rows(
                pricing_groups(conn, filters, [("date", "date"), ("model_name", "COALESCE(NULLIF(model,''),'Unknown')")], pricing),
                ["date", "model_name"], pricing,
            )
            daily_models = [
                {"date": r["date"], "model": r["model_name"], "api_cost": r["api_cost"], "total_tokens": r["total_tokens"]}
                for r in daily_rows
            ]
            runtime = runtime_breakdown(conn, filters, pricing)

            used_models = {str(r["name"]) for r in model_rows}
            price_models = []
            aliases = pricing.get("aliases") or {}
            for model in sorted(used_models):
                canonical = str(aliases.get(model.lower(), model.lower())) if isinstance(aliases, dict) else model.lower()
                rates = rates_for(model, pricing)
                price_models.append({"model": model, "canonical_model": canonical, "rates": rates, "priced": bool(rates)})

            return {
                "ready": True, "tab": "subscription", "filters": filters,
                "metadata": decode_metadata(conn), "summary": summary,
                "models": sorted(model_rows, key=lambda r: float(r.get("api_cost") or 0), reverse=True),
                "agents": sorted(agent_rows, key=lambda r: float(r.get("api_cost") or 0), reverse=True),
                "daily_models": daily_models,
                "runtime_summary": runtime["summary"],
                "runtime_agents": runtime["agents"],
                "runtime_daily": runtime["daily"],
                "pricing": {
                    "source_url": pricing.get("source_url"),
                    "as_of": pricing.get("as_of"),
                    "retrieved_at": pricing.get("retrieved_at"),
                    "status": pricing.get("status"),
                    "long_context_threshold": pricing.get("long_context_threshold"),
                    "used_models": price_models,
                },
            }
        finally:
            conn.close()

    return cached_payload(data_dir, "subscription", filters, (), build)


def insights_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    def build():
        conn = open_db(data_dir)
        if conn is None:
            return {"ready": False, "tab": "insights"}
        try:
            where, values = where_for(filters)
            summary = dict(conn.execute(
                f"""SELECT COUNT(*) responses,
                           COALESCE(SUM(input_tokens),0) input_tokens,
                           COALESCE(SUM(fresh_input_tokens),0) fresh_input_tokens,
                           COALESCE(AVG(input_tokens),0) avg_input,
                           COALESCE(SUM(CASE WHEN is_compaction='true' THEN total_tokens ELSE 0 END),0) compaction_tokens,
                           COALESCE(SUM(CASE WHEN is_compaction='true' THEN estimated_credits ELSE 0 END),0) compaction_credits,
                           SUM(CASE WHEN usage_source='token_usage_record' THEN 1 ELSE 0 END) direct_usage_responses
                      FROM responses{where}""",
                values,
            ).fetchone())
            summary["p95_input"] = percentile(conn, "responses", "input_tokens", where, values, .95)
            summary["compaction_attribution_coverage_pct"] = (
                float(summary["direct_usage_responses"] or 0) / float(summary["responses"] or 1) * 100.0
                if summary["responses"] else 0.0
            )
            fresh = float(summary["fresh_input_tokens"] or 0)
            summary["context_amplification"] = float(summary["input_tokens"] or 0) / fresh if fresh else 0.0

            turn_where, turn_values = where_for(filters)
            turns = dict(conn.execute(
                f"""SELECT COUNT(*) turns,
                           COALESCE(AVG(duration_ms),0) avg_duration_ms,
                           COALESCE(AVG(time_to_first_token_ms),0) avg_ttft_ms,
                           SUM(CASE WHEN status IN ('failed','aborted') THEN 1 ELSE 0 END) failed_turns
                      FROM turns{turn_where}""",
                turn_values,
            ).fetchone())
            turns["failure_rate_pct"] = float(turns["failed_turns"] or 0) / float(turns["turns"] or 1) * 100.0 if turns["turns"] else 0.0
            turns["p95_duration_ms"] = percentile(conn, "turns", "duration_ms", turn_where, turn_values, .95)
            turns["p95_ttft_ms"] = percentile(conn, "turns", "time_to_first_token_ms", turn_where, turn_values, .95)
            turns["p95_context_pct"] = percentile(conn, "turns", "context_utilization_pct", turn_where, turn_values, .95)

            act_where, act_values = where_for(filters)
            skill_summary = dict(conn.execute(
                f"""SELECT COUNT(*) skill_invocations,
                           COUNT(DISTINCT CASE WHEN skill_name<>'' THEN skill_name END) distinct_skills
                      FROM activities{act_where}
                     {'AND' if act_where else 'WHERE'} activity_type='skill'""",
                act_values,
            ).fetchone())

            limit_filters = {k: v for k, v in filters.items() if k in {"from", "to", "model"}}
            limit_where, limit_values = where_for(limit_filters)
            latest = conn.execute(
                f"SELECT * FROM rate_limits{limit_where} ORDER BY timestamp_utc DESC LIMIT 1",
                limit_values,
            ).fetchone()

            return {
                "ready": True, "tab": "insights", "filters": filters,
                "metadata": decode_metadata(conn), "summary": summary,
                "turn_summary": turns, "skill_summary": skill_summary,
                "latest_rate_limit": dict(latest) if latest else None,
                "code_metric_note": "Codex rollouts do not provide a reliable lines-of-code-generated metric. Patch diffs are counted when explicitly persisted, but shell/file writes are not consistently attributable, so the dashboard does not fabricate a generated-LOC number.",
            }
        finally:
            conn.close()

    return cached_payload(data_dir, "insights", filters, (), build)


def activity_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    pricing = load_pricing(data_dir)

    def build():
        conn = open_db(data_dir)
        if conn is None:
            return {"ready": False, "tab": "activity"}
        try:
            where, values = where_for(filters)
            daily_models = rows_as_dicts(conn.execute(
                f"""SELECT date,COALESCE(NULLIF(model,''),'Unknown') model,SUM(total_tokens) total_tokens
                      FROM responses{where}
                     GROUP BY date,model ORDER BY date,model""",
                values,
            ).fetchall())

            turn_where, turn_values = where_for(filters)
            turns_by_model = rows_as_dicts(conn.execute(
                f"""SELECT date,COALESCE(NULLIF(model,''),'Unknown') model,COUNT(*) turns
                      FROM turns{turn_where}
                     GROUP BY date,model ORDER BY date,model""",
                turn_values,
            ).fetchall())

            runtime = runtime_breakdown(conn, filters, pricing)
            runtime_summary = runtime["summary"]
            runtime_daily = runtime["daily"]
            runtime_by_agent = runtime["by_agent_daily"]

            act_where, act_values = where_for(filters)
            skill_activity = rows_as_dicts(conn.execute(
                f"""SELECT date,skill_name name,COUNT(*) value
                      FROM activities{act_where}
                     {'AND' if act_where else 'WHERE'} activity_type='skill' AND skill_name<>''
                     GROUP BY date,skill_name ORDER BY date,skill_name""",
                act_values,
            ).fetchall())
            skill_totals = rows_as_dicts(conn.execute(
                f"""SELECT skill_name name,COUNT(*) value
                      FROM activities{act_where}
                     {'AND' if act_where else 'WHERE'} activity_type='skill' AND skill_name<>''
                     GROUP BY skill_name ORDER BY value DESC,name""",
                act_values,
            ).fetchall())
            skill_invocations = sum(int(r["value"] or 0) for r in skill_totals)

            mcp_integrations = rows_as_dicts(conn.execute(
                f"""SELECT plugin_name name,COUNT(*) calls,
                           COUNT(DISTINCT CASE WHEN tool_name<>'' THEN tool_name END) distinct_tools,
                           COALESCE(SUM(duration_ms),0) duration_ms
                      FROM activities{act_where}
                     {'AND' if act_where else 'WHERE'} activity_type='plugin' AND plugin_name<>''
                     GROUP BY plugin_name
                     ORDER BY calls DESC,name""",
                act_values,
            ).fetchall())
            mcp_tools = rows_as_dicts(conn.execute(
                f"""SELECT plugin_name mcp,tool_name tool,COUNT(*) calls,
                           COALESCE(SUM(duration_ms),0) duration_ms
                      FROM activities{act_where}
                     {'AND' if act_where else 'WHERE'} activity_type='plugin'
                       AND plugin_name<>'' AND tool_name<>''
                     GROUP BY plugin_name,tool_name
                     ORDER BY calls DESC,mcp,tool""",
                act_values,
            ).fetchall())

            selected = {str(r["name"]).lower() for r in skill_totals}
            historical = {
                str(r[0]).lower()
                for r in conn.execute("SELECT DISTINCT skill_name FROM activities WHERE activity_type='skill' AND skill_name<>''").fetchall()
                if r[0]
            }
            configured = []
            try:
                skill_rows = conn.execute("SELECT name FROM configured_skills ORDER BY name").fetchall()
            except sqlite3.Error:
                skill_rows = []
            for row in skill_rows:
                name = str(row[0] or "")
                key = name.lower()
                configured.append({
                    "name": name,
                    "status": "used_selected" if key in selected else ("used_historical" if key in historical else "never_seen"),
                })

            return {
                "ready": True, "tab": "activity", "filters": filters,
                "metadata": decode_metadata(conn), "daily_models": daily_models,
                "turns_by_model": turns_by_model,
                "runtime_summary": runtime_summary,
                "runtime_daily": runtime_daily,
                "runtime_by_agent": runtime_by_agent,
                "mcp_integrations": mcp_integrations,
                "mcp_tools": mcp_tools,
                "skill_activity": skill_activity,
                "skill_totals": skill_totals, "skill_invocations": skill_invocations,
                "distinct_skills": len(skill_totals), "configured_skills": configured,
                "code_metric_note": "Patch lines were removed from the dashboard because the local rollout format does not capture all code-writing paths consistently. A trustworthy generated-lines metric would require Git/repository diff correlation rather than rollout counting alone.",
            }
        finally:
            conn.close()

    return cached_payload(data_dir, "activity", filters, (), build)


def sessions_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    def build():
        conn = open_db(data_dir)
        if conn is None:
            return {"ready": False, "tab": "sessions"}
        pricing = load_pricing(data_dir)
        try:
            where, values = where_for(filters)
            session_rows = rows_as_dicts(conn.execute(
                f"""SELECT session_id,
                           MAX(CASE WHEN session_name<>'' THEN session_name ELSE NULL END) name,
                           MIN(NULLIF(thread_start_local,'')) start,
                           MAX(NULLIF(thread_latest_local,'')) latest,
                           COUNT(*) responses,
                           COUNT(DISTINCT CASE WHEN agent_type='Subagent' THEN thread_id END) agents,
                           SUM(CASE WHEN is_compaction='true' THEN 1 ELSE 0 END) compactions,
                           SUM(fresh_input_tokens) fresh,
                           SUM(cached_input_tokens) cached,
                           SUM(output_tokens) output,
                           SUM(total_tokens) total,
                           SUM(estimated_credits) credits,
                           SUM(CASE WHEN credit_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END) credit_priced
                      FROM responses{where}
                     {'AND' if where else 'WHERE'} session_id<>''
                     GROUP BY session_id
                     ORDER BY total DESC
                     LIMIT 100""",
                values,
            ).fetchall())
            ids = [str(r["session_id"]) for r in session_rows]
            if not ids:
                return {"ready": True, "tab": "sessions", "filters": filters, "metadata": decode_metadata(conn), "sessions": []}
            placeholders = ",".join("?" for _ in ids)
            limited_where = f"{where} {'AND' if where else 'WHERE'} session_id IN ({placeholders})"
            limited_values = [*values, *ids]

            top_models = {}
            for row in conn.execute(
                f"""SELECT session_id,model,SUM(total_tokens) total
                      FROM responses{limited_where}
                     GROUP BY session_id,model ORDER BY session_id,total DESC""",
                limited_values,
            ).fetchall():
                sid = str(row["session_id"])
                if sid not in top_models:
                    top_models[sid] = str(row["model"] or "Unknown")

            turn_where, turn_values = where_for(filters)
            turn_limited = f"{turn_where} {'AND' if turn_where else 'WHERE'} session_id IN ({placeholders})"
            turn_map = {
                str(row["session_id"]): dict(row)
                for row in conn.execute(
                    f"""SELECT session_id,COUNT(*) turns,COALESCE(SUM(duration_ms),0) duration,
                               SUM(CASE WHEN status IN ('failed','aborted') THEN 1 ELSE 0 END) failures,
                               COALESCE(MAX(context_utilization_pct),0) max_context
                          FROM turns{turn_limited}
                         GROUP BY session_id""",
                    [*turn_values, *ids],
                ).fetchall()
            }

            cost_rows = price_group_rows(
                pricing_groups(
                    conn, filters, [("session_id", "session_id")], pricing,
                    f"session_id IN ({placeholders})", ids,
                ),
                ["session_id"], pricing,
            )
            cost_map = {str(r["session_id"]): r for r in cost_rows if str(r["session_id"]) in set(ids)}

            sessions = []
            for row in session_rows:
                sid = str(row["session_id"])
                tr = turn_map.get(sid, {})
                cr = cost_map.get(sid, {})
                total = float(row.get("total") or 0)
                sessions.append({
                    **row,
                    "name": row.get("name") or sid,
                    "top_model": top_models.get(sid, "Unknown"),
                    "turns": int(tr.get("turns") or 0),
                    "duration": float(tr.get("duration") or 0),
                    "failures": int(tr.get("failures") or 0),
                    "max_context": float(tr.get("max_context") or 0),
                    "credit_coverage": float(row.get("credit_priced") or 0) / total * 100.0 if total else 0.0,
                    "api_cost": float(cr.get("api_cost") or 0),
                    "api_coverage": float(cr.get("priced_tokens") or 0) / total * 100.0 if total else 0.0,
                })

            return {
                "ready": True, "tab": "sessions", "filters": filters,
                "metadata": decode_metadata(conn), "sessions": sessions,
                "note": "Top 100 sessions by total token usage in the current filters; table sorting is applied to this result set.",
            }
        finally:
            conn.close()

    return cached_payload(data_dir, "sessions", filters, (), build)


TAB_BUILDERS = {
    "token": token_payload,
    "subscription": subscription_payload,
    "insights": insights_payload,
    "activity": activity_payload,
    "sessions": sessions_payload,
}


def dashboard_payload(data_dir: Path, filters: dict[str, str], tab: str) -> dict[str, object]:
    builder = TAB_BUILDERS.get(tab)
    if builder is None:
        raise ValueError(f"Unknown dashboard tab: {tab}")
    return builder(data_dir, filters)


def refresh_status() -> dict[str, object]:
    with _REFRESH_LOCK:
        return json.loads(json.dumps(_REFRESH))


def start_refresh(data_dir: Path, codex_home: Path, request: dict[str, object]) -> tuple[bool, dict[str, object]]:
    with _REFRESH_LOCK:
        if _REFRESH.get("status") == "running":
            return False, dict(_REFRESH)
        job_id = str(uuid.uuid4())
        _REFRESH.update({
            "id": job_id, "status": "running", "started_at": utc_now(),
            "finished_at": None, "request": request,
            "message": "Preparing refresh…", "phase": "starting", "log": [], "return_code": None,
        })

    def worker() -> None:
        cmd = [
            sys.executable, "-u", str(ROOT / "collect_codex_usage.py"),
            "--codex-home", str(codex_home), "--output-dir", str(data_dir),
            "--sqlite-only",
        ]
        if request.get("mode") == "range":
            cmd += ["--from-date", str(request["from"]), "--to-date", str(request["to"])]
        else:
            cmd += ["--days", str(int(request.get("days") or 7))]
        if request.get("scan_all"):
            cmd.append("--scan-all")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            log_lines: list[str] = []
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    log_lines.append(line)
                    log_lines = log_lines[-40:]
                    with _REFRESH_LOCK:
                        _REFRESH["log"] = list(log_lines)
                        _REFRESH["message"] = line
                        if line.startswith("Preparing"):
                            _REFRESH["phase"] = "preparing"
                        elif line.startswith("Scanning") or line.startswith("Parsed"):
                            _REFRESH["phase"] = "scanning"
                        elif line.startswith("Updating selected SQLite"):
                            _REFRESH["phase"] = "updating"
                        elif line.startswith("Refresh complete"):
                            _REFRESH["phase"] = "finalising"
            return_code = proc.wait()
            clear_cache()
            with _REFRESH_LOCK:
                _REFRESH["return_code"] = return_code
                _REFRESH["log"] = log_lines
                _REFRESH["finished_at"] = utc_now()
                if return_code == 0:
                    _REFRESH["status"] = "completed"
                    _REFRESH["phase"] = "completed"
                    _REFRESH["message"] = "Refresh completed."
                else:
                    _REFRESH["status"] = "failed"
                    _REFRESH["phase"] = "failed"
                    _REFRESH["message"] = f"Refresh failed with exit code {return_code}."
        except Exception as exc:
            with _REFRESH_LOCK:
                _REFRESH["status"] = "failed"
                _REFRESH["phase"] = "failed"
                _REFRESH["finished_at"] = utc_now()
                _REFRESH["message"] = str(exc)
                _REFRESH["log"] = traceback.format_exc().splitlines()[-40:]

    threading.Thread(target=worker, daemon=True, name=f"refresh-{job_id[:8]}").start()
    return True, refresh_status()


def migrate_existing_database(data_dir: Path, codex_home: Path) -> None:
    """Apply lightweight schema/index upgrades without rebuilding historical telemetry."""
    path = db_path(data_dir)
    if not path.exists():
        return
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        names = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"responses", "turns", "activities", "configured_agents", "metadata"}
        if not required <= names:
            return

        conn.execute('CREATE TABLE IF NOT EXISTS configured_skills ("name" TEXT, "skill_file" TEXT)')
        skills = collector.load_configured_skills(codex_home, False) if codex_home.exists() else []
        conn.execute("DELETE FROM configured_skills")
        if skills:
            conn.executemany(
                'INSERT INTO configured_skills ("name","skill_file") VALUES (?,?)',
                [(str(row.get("name") or ""), str(row.get("skill_file") or "")) for row in skills],
            )

        indexes = [
            ("idx_responses_date", "responses", "date"),
            ("idx_responses_model", "responses", "model"),
            ("idx_responses_agent", "responses", "agent_type, agent_role, agent_label"),
            ("idx_responses_effort", "responses", "reasoning_effort"),
            ("idx_responses_project", "responses", "project"),
            ("idx_responses_session", "responses", "session_id"),
            ("idx_responses_date_session", "responses", "date, session_id"),
            ("idx_responses_thread", "responses", "thread_id"),
            ("idx_responses_dashboard", "responses", "date, model, agent_type, agent_role, reasoning_effort, project"),
            ("idx_turns_date", "turns", "date"),
            ("idx_turns_session", "turns", "session_id"),
            ("idx_turns_date_session", "turns", "date, session_id"),
            ("idx_turns_model", "turns", "model"),
            ("idx_turns_dashboard", "turns", "date, model, agent_type, agent_role, reasoning_effort, project"),
            ("idx_activities_date", "activities", "date"),
            ("idx_activities_session", "activities", "session_id"),
            ("idx_activities_dashboard", "activities", "date, activity_type, model, agent_type, agent_role, reasoning_effort, project, skill_name"),
            ("idx_limits_date", "rate_limits", "date"),
        ]
        for name, table, cols in indexes:
            if table not in names:
                continue
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})')
        conn.execute("ANALYZE")
        conn.commit()
        clear_cache()
        print("SQLite schema/index maintenance completed.", flush=True)
    except Exception as exc:
        print(f"Warning: SQLite startup maintenance failed: {exc}", file=sys.stderr, flush=True)
    finally:
        conn.close()


def prepare_database_background(data_dir: Path, codex_home: Path) -> None:
    """Keep startup non-blocking; maintain/migrate data after HTTP is already serving."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if db_path(data_dir).exists():
        migrate_existing_database(data_dir, codex_home)
        return
    if not (data_dir / "codex_usage_records.csv").exists() or not codex_home.exists():
        return
    proc = subprocess.run(
        [
            sys.executable, str(ROOT / "collect_codex_usage.py"),
            "--days", "1", "--codex-home", str(codex_home), "--output-dir", str(data_dir),
        ],
        cwd=ROOT, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        print("Warning: initial SQLite build failed:\n" + (proc.stderr or proc.stdout or ""), file=sys.stderr, flush=True)
    else:
        clear_cache()
        print("Initial SQLite database build completed.", flush=True)


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "CodexUsageDashboard/2.0"

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    @property
    def app_server(self):
        return self.server

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: object, status: int = 200):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "") and len(raw) > 1024
        body = gzip.compress(raw, compresslevel=5) if use_gzip else raw
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/meta":
            self.send_json(meta_payload(self.app_server.data_dir))
            return
        if parsed.path == "/api/dashboard":
            try:
                query = parse_qs(parsed.query)
                filters = parse_filters(query)
                tab = (query.get("tab") or ["token"])[0]
                self.send_json(dashboard_payload(self.app_server.data_dir, filters, tab))
            except (ValueError, sqlite3.Error) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/pricing":
            self.send_json(load_pricing(self.app_server.data_dir))
            return
        if parsed.path == "/api/refresh/status":
            self.send_json(refresh_status())
            return
        if parsed.path == "/health":
            self.send_json({"ok": True, "database": db_path(self.app_server.data_dir).exists()})
            return
        if parsed.path in {"", "/"}:
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/pricing/refresh":
            try:
                payload = fetch_latest_pricing(self.app_server.data_dir)
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path != "/api/refresh":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 16384)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            mode = str(body.get("mode") or "days")
            scan_all = bool(body.get("scan_all"))
            if mode == "range":
                start = str(body.get("from") or "")
                end = str(body.get("to") or "")
                date.fromisoformat(start)
                date.fromisoformat(end)
                if end < start:
                    raise ValueError("The end date must be on or after the start date.")
                request = {"mode": "range", "from": start, "to": end, "scan_all": scan_all}
            else:
                days = int(body.get("days") or 7)
                if days < 1 or days > 3650:
                    raise ValueError("Days must be between 1 and 3650.")
                request = {"mode": "days", "days": days, "scan_all": scan_all}
            started, state = start_refresh(self.app_server.data_dir, self.app_server.codex_home, request)
            self.send_json(state, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, data_dir: Path, codex_home: Path):
        self.data_dir = data_dir
        self.codex_home = codex_home
        super().__init__(address, handler)


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    server = DashboardServer((args.host, args.port), DashboardHandler, data_dir, codex_home)
    print(f"Codex usage dashboard: http://127.0.0.1:{args.port}/", flush=True)
    print(f"Codex source (read-only mount recommended): {codex_home}", flush=True)
    print(f"Analytics database: {db_path(data_dir)}", flush=True)
    threading.Thread(
        target=prepare_database_background,
        args=(data_dir, codex_home),
        daemon=True,
        name="startup-database-maintenance",
    ).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
