#!/usr/bin/env python3
"""Normalized provider-agnostic usage schema shared by all collectors.

The existing Codex tables remain intact for the detailed Codex views. This
module projects those records, plus Paperclip run telemetry, into additive
normalized tables used by the cross-provider dashboard.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SOURCE_CODEX = "codex_desktop"
SOURCE_PAPERCLIP = "paperclip"

USAGE_RECORD_COLUMNS = [
    "record_key", "record_grain", "source_system", "source_record_id",
    "run_key", "run_id", "session_id", "date", "timestamp_utc",
    "started_utc", "finished_utc", "duration_ms",
    "agent_id", "agent_name", "agent_role",
    "provider", "runtime", "model", "billing_mode",
    "account_connection_id", "account_display_name",
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "fresh_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "token_metrics_available",
    "actual_provider_cost_usd", "actual_cost_available",
    "estimated_api_cost_usd", "estimated_cost_status",
    "task_id", "project_id", "project_name",
    "reasoning_effort", "service_tier", "status", "source_detail",
    "is_primary", "duplicate_of",
]

USAGE_RUN_COLUMNS = [
    "run_key", "source_system", "run_id", "session_id", "date",
    "started_utc", "finished_utc", "duration_ms",
    "agent_id", "agent_name", "agent_role",
    "provider", "runtime", "model", "billing_mode",
    "account_connection_id", "account_display_name",
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "fresh_input_tokens", "output_tokens", "reasoning_output_tokens",
    "total_tokens", "token_metrics_available",
    "actual_provider_cost_usd", "actual_cost_available",
    "estimated_api_cost_usd", "estimated_cost_status",
    "task_id", "project_id", "project_name",
    "reasoning_effort", "service_tier", "status", "source_detail",
    "is_primary", "duplicate_of",
]

INTEGER_COLUMNS = {
    "duration_ms", "input_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "fresh_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "token_metrics_available",
    "actual_cost_available", "is_primary",
}
REAL_COLUMNS = {"actual_provider_cost_usd", "estimated_api_cost_usd"}


def _sql_type(name: str) -> str:
    if name in INTEGER_COLUMNS:
        return "INTEGER"
    if name in REAL_COLUMNS:
        return "REAL"
    return "TEXT"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _ensure_table(conn: sqlite3.Connection, table: str, columns: list[str], pk: str) -> None:
    definitions = []
    for name in columns:
        suffix = " PRIMARY KEY" if name == pk else ""
        definitions.append(f'"{name}" {_sql_type(name)}{suffix}')
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(definitions)})')
    existing = _table_columns(conn, table)
    for name in columns:
        if name not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {_sql_type(name)}')


def ensure_schema(conn_or_path: sqlite3.Connection | Path | str) -> None:
    owns = not isinstance(conn_or_path, sqlite3.Connection)
    conn = (
        sqlite3.connect(str(conn_or_path), timeout=60)
        if owns
        else conn_or_path
    )
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        _ensure_table(conn, "usage_records", USAGE_RECORD_COLUMNS, "record_key")
        _ensure_table(conn, "usage_runs", USAGE_RUN_COLUMNS, "run_key")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_accounts (
                account_key TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                provider TEXT NOT NULL,
                billing_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_status (
                source_system TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                company_id TEXT,
                records_upserted INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS provider_quotas (
                quota_key TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                provider TEXT NOT NULL,
                account_connection_id TEXT,
                label TEXT NOT NULL,
                used_percent REAL,
                resets_at TEXT,
                value_label TEXT,
                detail TEXT,
                observed_at TEXT NOT NULL
            )"""
        )
        indexes = [
            ("idx_usage_records_date", "usage_records", "date"),
            ("idx_usage_records_source", "usage_records", "source_system"),
            ("idx_usage_records_provider", "usage_records", "provider"),
            ("idx_usage_records_billing", "usage_records", "billing_mode"),
            ("idx_usage_records_account", "usage_records", "account_connection_id"),
            ("idx_usage_records_agent", "usage_records", "agent_id"),
            ("idx_usage_records_model", "usage_records", "model"),
            ("idx_usage_records_primary", "usage_records", "is_primary,date"),
            ("idx_usage_records_session", "usage_records", "session_id"),
            ("idx_usage_runs_date", "usage_runs", "date"),
            ("idx_usage_runs_source", "usage_runs", "source_system"),
            ("idx_usage_runs_provider", "usage_runs", "provider"),
            ("idx_usage_runs_billing", "usage_runs", "billing_mode"),
            ("idx_usage_runs_account", "usage_runs", "account_connection_id"),
            ("idx_usage_runs_agent", "usage_runs", "agent_id"),
            ("idx_usage_runs_model", "usage_runs", "model"),
            ("idx_usage_runs_primary", "usage_runs", "is_primary,date"),
            ("idx_usage_runs_session", "usage_runs", "session_id"),
        ]
        for name, table, cols in indexes:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})')
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-") or "unknown"


def _codex_agent(row: sqlite3.Row | dict[str, Any]) -> tuple[str, str, str]:
    kind = str(row["agent_type"] or "")
    if kind == "Main":
        return "codex:main", "Main", "Main"
    role = str(row["agent_role"] or row["agent_label"] or "Subagent")
    return f"codex:role:{_slug(role)}", role, role


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: Iterable[dict[str, Any]],
) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    names = ",".join(f'"{name}"' for name in columns)
    placeholders = ",".join("?" for _ in columns)
    values = [
        [row.get(name, 0 if name in INTEGER_COLUMNS else None if name in REAL_COLUMNS else "") for name in columns]
        for row in materialized
    ]
    conn.executemany(
        f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})',
        values,
    )
    return len(materialized)


def replace_source_dates(
    conn: sqlite3.Connection,
    source_system: str,
    dates: set[str],
    records: Iterable[dict[str, Any]],
    runs: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    ensure_schema(conn)
    if dates:
        placeholders = ",".join("?" for _ in dates)
        args = [source_system, *sorted(dates)]
        conn.execute(
            f'DELETE FROM usage_records WHERE source_system=? AND date IN ({placeholders})',
            args,
        )
        conn.execute(
            f'DELETE FROM usage_runs WHERE source_system=? AND date IN ({placeholders})',
            args,
        )
    record_count = _insert_rows(conn, "usage_records", USAGE_RECORD_COLUMNS, records)
    run_count = _insert_rows(conn, "usage_runs", USAGE_RUN_COLUMNS, runs)
    return record_count, run_count


def upsert_account(
    conn: sqlite3.Connection,
    source_system: str,
    connection_id: str,
    display_name: str,
    provider: str,
    billing_mode: str,
    status: str = "active",
    seen_at: str | None = None,
) -> None:
    ensure_schema(conn)
    key = f"{source_system}:{connection_id}"
    conn.execute(
        """INSERT INTO usage_accounts(
               account_key,source_system,connection_id,display_name,provider,billing_mode,status,last_seen_at
           ) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(account_key) DO UPDATE SET
               display_name=excluded.display_name,
               provider=excluded.provider,
               billing_mode=excluded.billing_mode,
               status=excluded.status,
               last_seen_at=excluded.last_seen_at""",
        (
            key, source_system, connection_id, display_name, provider,
            billing_mode, status, seen_at or _utc_now(),
        ),
    )


def set_source_status(
    conn: sqlite3.Connection,
    source_system: str,
    status: str,
    message: str,
    *,
    company_id: str = "",
    records_upserted: int = 0,
    details_json: str = "{}",
    success: bool = False,
) -> None:
    ensure_schema(conn)
    now = _utc_now()
    previous = conn.execute(
        "SELECT last_success_at FROM source_status WHERE source_system=?",
        (source_system,),
    ).fetchone()
    last_success = now if success else (previous[0] if previous else None)
    conn.execute(
        """INSERT INTO source_status(
               source_system,status,message,last_attempt_at,last_success_at,
               company_id,records_upserted,details_json
           ) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source_system) DO UPDATE SET
               status=excluded.status,
               message=excluded.message,
               last_attempt_at=excluded.last_attempt_at,
               last_success_at=excluded.last_success_at,
               company_id=excluded.company_id,
               records_upserted=excluded.records_upserted,
               details_json=excluded.details_json""",
        (
            source_system, status, message, now, last_success,
            company_id, int(records_upserted), details_json,
        ),
    )


def mark_cross_source_duplicates(conn: sqlite3.Connection) -> None:
    """Prefer Paperclip run attribution when a durable session id proves overlap.

    Codex remains queryable when explicitly filtering to that source. Default
    cross-source queries use is_primary=1, preventing a Paperclip-managed Codex
    workload from being counted twice when both collectors observe the same
    underlying provider session.
    """
    ensure_schema(conn)
    conn.execute(
        "UPDATE usage_records SET is_primary=1, duplicate_of='' WHERE source_system=?",
        (SOURCE_CODEX,),
    )
    conn.execute(
        "UPDATE usage_runs SET is_primary=1, duplicate_of='' WHERE source_system=?",
        (SOURCE_CODEX,),
    )

    paperclip_runs = conn.execute(
        """SELECT run_key,session_id,token_metrics_available
             FROM usage_runs
            WHERE source_system=? AND session_id<>''""",
        (SOURCE_PAPERCLIP,),
    ).fetchall()
    for run_key, session_id, token_available in paperclip_runs:
        # Run-level Paperclip attribution is authoritative whenever the session
        # identity matches, even if the runtime omitted token counters.
        conn.execute(
            """UPDATE usage_runs
                  SET is_primary=0,duplicate_of=?
                WHERE source_system=? AND session_id=?""",
            (run_key, SOURCE_CODEX, session_id),
        )
        # Suppress Codex response tokens only when Paperclip itself has token
        # metrics; otherwise retain the available Codex token evidence.
        if int(token_available or 0):
            conn.execute(
                """UPDATE usage_records
                      SET is_primary=0,duplicate_of=?
                    WHERE source_system=? AND session_id=?""",
                (run_key, SOURCE_CODEX, session_id),
            )


def sync_codex_usage(
    db_path: Path | str,
    selected_dates: set[str] | None = None,
) -> dict[str, int]:
    path = Path(db_path)
    if not path.exists():
        return {"records": 0, "runs": 0}
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        ensure_schema(conn)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"responses", "turns"} <= tables:
            return {"records": 0, "runs": 0}

        where = ""
        values: list[Any] = []
        if selected_dates:
            placeholders = ",".join("?" for _ in selected_dates)
            where = f" WHERE date IN ({placeholders})"
            values = sorted(selected_dates)
            dates = set(selected_dates)
        else:
            dates = {
                str(row[0])
                for row in conn.execute("SELECT DISTINCT date FROM responses WHERE date<>''")
            }
            dates.update(
                str(row[0])
                for row in conn.execute("SELECT DISTINCT date FROM turns WHERE date<>''")
            )
            conn.execute("DELETE FROM usage_records WHERE source_system=?", (SOURCE_CODEX,))
            conn.execute("DELETE FROM usage_runs WHERE source_system=?", (SOURCE_CODEX,))

        records: list[dict[str, Any]] = []
        for row in conn.execute(f"SELECT * FROM responses{where}", values):
            agent_id, agent_name, agent_role = _codex_agent(row)
            source_input = int(row["input_tokens"] or 0)
            cached = int(row["cached_input_tokens"] or 0)
            cache_write = int(row["cache_write_input_tokens"] or 0)
            ordinary = max(0, source_input - cached - cache_write)
            fresh = int(row["fresh_input_tokens"] or max(0, source_input - cached))
            thread_id = str(row["thread_id"] or "")
            response_id = str(row["response_id"] or "")
            turn_id = str(row["turn_id"] or "")
            run_key = f"{SOURCE_CODEX}:turn:{thread_id}:{turn_id}"
            records.append({
                "record_key": f"{SOURCE_CODEX}:response:{thread_id}:{response_id}",
                "record_grain": "response",
                "source_system": SOURCE_CODEX,
                "source_record_id": response_id,
                "run_key": run_key,
                "run_id": turn_id,
                "session_id": str(row["session_id"] or thread_id),
                "date": str(row["date"] or ""),
                "timestamp_utc": str(row["timestamp_utc"] or ""),
                "started_utc": "",
                "finished_utc": str(row["timestamp_utc"] or ""),
                "duration_ms": 0,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "agent_role": agent_role,
                "provider": "openai",
                "runtime": "codex",
                "model": str(row["model"] or ""),
                "billing_mode": "subscription",
                "account_connection_id": "codex-desktop",
                "account_display_name": "Codex Desktop",
                "input_tokens": ordinary,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": cache_write,
                "fresh_input_tokens": fresh,
                "output_tokens": int(row["output_tokens"] or 0),
                "reasoning_output_tokens": int(row["reasoning_output_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "token_metrics_available": 1,
                "actual_provider_cost_usd": None,
                "actual_cost_available": 0,
                "estimated_api_cost_usd": float(row["api_equivalent_cost_usd"] or 0),
                "estimated_cost_status": str(row["api_cost_rate_status"] or ""),
                "task_id": "",
                "project_id": "",
                "project_name": str(row["project"] or ""),
                "reasoning_effort": str(row["reasoning_effort"] or ""),
                "service_tier": str(row["service_tier"] or ""),
                "status": "completed",
                "source_detail": str(row["usage_source"] or "codex_rollout"),
                "is_primary": 1,
                "duplicate_of": "",
            })

        cost_by_turn: dict[tuple[str, str], float] = {}
        for row in conn.execute(
            f"""SELECT thread_id,turn_id,COALESCE(SUM(api_equivalent_cost_usd),0)
                  FROM responses{where}
                 GROUP BY thread_id,turn_id""",
            values,
        ):
            cost_by_turn[(str(row[0] or ""), str(row[1] or ""))] = float(row[2] or 0)

        runs: list[dict[str, Any]] = []
        for row in conn.execute(f"SELECT * FROM turns{where}", values):
            agent_id, agent_name, agent_role = _codex_agent(row)
            thread_id = str(row["thread_id"] or "")
            turn_id = str(row["turn_id"] or "")
            source_input = int(row["input_tokens"] or 0)
            cached = int(row["cached_input_tokens"] or 0)
            fresh = int(row["fresh_input_tokens"] or max(0, source_input - cached))
            ordinary = max(0, source_input - cached)
            responses = int(row["responses"] or 0)
            runs.append({
                "run_key": f"{SOURCE_CODEX}:turn:{thread_id}:{turn_id}",
                "source_system": SOURCE_CODEX,
                "run_id": turn_id,
                "session_id": str(row["session_id"] or thread_id),
                "date": str(row["date"] or ""),
                "started_utc": str(row["started_utc"] or ""),
                "finished_utc": str(row["completed_utc"] or ""),
                "duration_ms": int(row["duration_ms"] or 0),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "agent_role": agent_role,
                "provider": "openai",
                "runtime": "codex",
                "model": str(row["model"] or ""),
                "billing_mode": "subscription",
                "account_connection_id": "codex-desktop",
                "account_display_name": "Codex Desktop",
                "input_tokens": ordinary,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": 0,
                "fresh_input_tokens": fresh,
                "output_tokens": int(row["output_tokens"] or 0),
                "reasoning_output_tokens": int(row["reasoning_output_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "token_metrics_available": 1 if responses > 0 or int(row["total_tokens"] or 0) > 0 else 0,
                "actual_provider_cost_usd": None,
                "actual_cost_available": 0,
                "estimated_api_cost_usd": cost_by_turn.get((thread_id, turn_id), 0.0),
                "estimated_cost_status": "priced_from_responses" if (thread_id, turn_id) in cost_by_turn else "unavailable",
                "task_id": "",
                "project_id": "",
                "project_name": str(row["project"] or ""),
                "reasoning_effort": str(row["reasoning_effort"] or ""),
                "service_tier": str(row["service_tier"] or ""),
                "status": str(row["status"] or "unknown"),
                "source_detail": "codex_turn",
                "is_primary": 1,
                "duplicate_of": "",
            })

        if selected_dates:
            record_count, run_count = replace_source_dates(
                conn, SOURCE_CODEX, dates, records, runs
            )
        else:
            record_count = _insert_rows(
                conn, "usage_records", USAGE_RECORD_COLUMNS, records
            )
            run_count = _insert_rows(conn, "usage_runs", USAGE_RUN_COLUMNS, runs)

        upsert_account(
            conn, SOURCE_CODEX, "codex-desktop", "Codex Desktop",
            "openai", "subscription", "active",
        )
        mark_cross_source_duplicates(conn)
        set_source_status(
            conn, SOURCE_CODEX, "ok",
            f"Normalized {record_count:,} Codex response records and {run_count:,} turns.",
            records_upserted=record_count,
            success=True,
        )
        conn.commit()
        return {"records": record_count, "runs": run_count}
    finally:
        conn.close()
