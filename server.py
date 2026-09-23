#!/usr/bin/env python3
"""Local HTTP/API server for the Dockerized Codex usage dashboard."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"

_REFRESH_LOCK = threading.Lock()
_REFRESH: dict[str, object] = {
    "id": None,
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "request": None,
    "message": "",
    "log": [],
    "return_code": None,
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


def open_db(data_dir: Path) -> sqlite3.Connection | None:
    path = db_path(data_dir)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
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
        "model": one("model"),
        "agent": one("agent"),
        "effort": one("effort"),
        "project": one("project"),
    }
    for key in ("from", "to"):
        if result[key]:
            date.fromisoformat(result[key])
    return result


def where_for(filters: dict[str, str], alias: str = "") -> tuple[str, list[object]]:
    p = f"{alias}." if alias else ""
    clauses: list[str] = []
    values: list[object] = []
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
        clauses.append(f"{agent_expr(alias)}=?")
        values.append(filters["agent"])
    if filters.get("effort"):
        clauses.append(f"{p}reasoning_effort=?")
        values.append(filters["effort"])
    if filters.get("project"):
        clauses.append(f"{p}project=?")
        values.append(filters["project"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


def percentile(conn: sqlite3.Connection, table: str, field: str, where: str, values: list[object], p: float) -> float:
    count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"{where} AND "{field}">0' if where else f'SELECT COUNT(*) FROM "{table}" WHERE "{field}">0', values).fetchone()[0])
    if count <= 0:
        return 0.0
    offset = round((count - 1) * p)
    sql = f'SELECT "{field}" FROM "{table}"{where} AND "{field}">0' if where else f'SELECT "{field}" FROM "{table}" WHERE "{field}">0'
    row = conn.execute(sql + f' ORDER BY "{field}" LIMIT 1 OFFSET ?', [*values, offset]).fetchone()
    return float(row[0] or 0) if row else 0.0


def rows_as_dicts(rows) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def meta_payload(data_dir: Path) -> dict[str, object]:
    conn = open_db(data_dir)
    if conn is None:
        return {
            "ready": False,
            "data_min": None,
            "data_max": None,
            "models": [],
            "agents": [],
            "efforts": [],
            "projects": [],
            "metadata": {},
            "refresh": dict(_REFRESH),
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
            "refresh": dict(_REFRESH),
        }
    finally:
        conn.close()


def dashboard_payload(data_dir: Path, filters: dict[str, str]) -> dict[str, object]:
    conn = open_db(data_dir)
    if conn is None:
        return {"ready": False, "filters": filters}

    try:
        where, values = where_for(filters)

        summary_sql = f"""
            SELECT
              COUNT(*) responses,
              COUNT(DISTINCT CASE WHEN session_id<>'' THEN session_id END) sessions,
              COUNT(DISTINCT CASE WHEN thread_id<>'' THEN thread_id END) threads,
              COALESCE(SUM(input_tokens),0) input_tokens,
              COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
              COALESCE(SUM(fresh_input_tokens),0) fresh_input_tokens,
              COALESCE(SUM(output_tokens),0) output_tokens,
              COALESCE(SUM(reasoning_output_tokens),0) reasoning_output_tokens,
              COALESCE(SUM(total_tokens),0) total_tokens,
              COALESCE(SUM(estimated_credits),0) estimated_credits,
              COALESCE(SUM(api_equivalent_cost_usd),0) api_cost,
              COALESCE(SUM(CASE WHEN agent_type='Subagent' THEN total_tokens ELSE 0 END),0) subagent_tokens,
              COALESCE(SUM(CASE WHEN is_compaction='true' THEN total_tokens ELSE 0 END),0) compaction_tokens,
              COALESCE(SUM(CASE WHEN is_compaction='true' THEN estimated_credits ELSE 0 END),0) compaction_credits,
              SUM(CASE WHEN is_compaction='true' THEN 1 ELSE 0 END) compactions,
              SUM(CASE WHEN api_long_context='true' THEN 1 ELSE 0 END) long_context_responses,
              COALESCE(SUM(CASE WHEN credit_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END),0) credit_priced_tokens,
              COALESCE(SUM(CASE WHEN api_cost_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END),0) api_priced_tokens,
              COALESCE(AVG(input_tokens),0) avg_input
            FROM responses{where}
        """
        summary = dict(conn.execute(summary_sql, values).fetchone())
        total_tokens = float(summary["total_tokens"] or 0)
        summary["cache_hit_pct"] = (float(summary["cached_input_tokens"] or 0) / float(summary["input_tokens"] or 1) * 100.0) if summary["input_tokens"] else 0.0
        summary["subagent_share_pct"] = float(summary["subagent_tokens"] or 0) / total_tokens * 100.0 if total_tokens else 0.0
        summary["credit_coverage_pct"] = float(summary["credit_priced_tokens"] or 0) / total_tokens * 100.0 if total_tokens else 0.0
        summary["api_coverage_pct"] = float(summary["api_priced_tokens"] or 0) / total_tokens * 100.0 if total_tokens else 0.0
        summary["p95_input"] = percentile(conn, "responses", "input_tokens", where, values, .95)
        fresh = float(summary["fresh_input_tokens"] or 0)
        summary["context_amplification"] = float(summary["input_tokens"] or 0) / fresh if fresh else 0.0

        def grouped(dimension_sql: str, label: str) -> list[dict[str, object]]:
            sql = f"""
                SELECT {dimension_sql} AS name,
                       COUNT(*) responses,
                       COALESCE(SUM(total_tokens),0) total_tokens,
                       COALESCE(SUM(fresh_input_tokens),0) fresh_input_tokens,
                       COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                       COALESCE(SUM(output_tokens),0) output_tokens,
                       COALESCE(SUM(estimated_credits),0) estimated_credits,
                       COALESCE(SUM(api_equivalent_cost_usd),0) api_cost
                  FROM responses{where}
                 GROUP BY {dimension_sql}
                 ORDER BY total_tokens DESC
            """
            return rows_as_dicts(conn.execute(sql, values).fetchall())

        models = grouped("COALESCE(NULLIF(model,''),'Unknown')", "model")
        agents = grouped(agent_expr(), "agent")
        efforts = grouped("COALESCE(NULLIF(reasoning_effort,''),'Unknown')", "effort")
        agent_types = grouped("COALESCE(NULLIF(agent_type,''),'Unknown')", "agent_type")

        daily_models = rows_as_dicts(conn.execute(
            f"""SELECT date,COALESCE(NULLIF(model,''),'Unknown') model,
                       COUNT(*) responses,
                       SUM(total_tokens) total_tokens,
                       SUM(cached_input_tokens) cached_input_tokens,
                       SUM(fresh_input_tokens) fresh_input_tokens,
                       SUM(output_tokens) output_tokens,
                       SUM(estimated_credits) estimated_credits,
                       SUM(api_equivalent_cost_usd) api_cost
                  FROM responses{where}
                 GROUP BY date,model
                 ORDER BY date,model""",
            values,
        ).fetchall())

        turn_where, turn_values = where_for(filters)
        turn_summary = dict(conn.execute(
            f"""SELECT COUNT(*) turns,
                       COALESCE(AVG(duration_ms),0) avg_duration_ms,
                       COALESCE(AVG(time_to_first_token_ms),0) avg_ttft_ms,
                       SUM(CASE WHEN status IN ('failed','aborted') THEN 1 ELSE 0 END) failed_turns
                  FROM turns{turn_where}""",
            turn_values,
        ).fetchone())
        turn_summary["failure_rate_pct"] = float(turn_summary["failed_turns"] or 0) / float(turn_summary["turns"] or 1) * 100.0 if turn_summary["turns"] else 0.0
        turn_summary["p95_duration_ms"] = percentile(conn, "turns", "duration_ms", turn_where, turn_values, .95)
        turn_summary["p95_ttft_ms"] = percentile(conn, "turns", "time_to_first_token_ms", turn_where, turn_values, .95)
        turn_summary["p95_context_pct"] = percentile(conn, "turns", "context_utilization_pct", turn_where, turn_values, .95)

        turns_by_model = rows_as_dicts(conn.execute(
            f"""SELECT date,COALESCE(NULLIF(model,''),'Unknown') model,COUNT(*) turns
                  FROM turns{turn_where}
                 GROUP BY date,model
                 ORDER BY date,model""",
            turn_values,
        ).fetchall())

        act_where, act_values = where_for(filters)
        activity_summary = dict(conn.execute(
            f"""SELECT
                    SUM(CASE WHEN activity_type='tool' THEN 1 ELSE 0 END) tool_calls,
                    COUNT(DISTINCT CASE WHEN activity_type='tool' AND plugin_name<>'' THEN plugin_name END) distinct_plugins,
                    SUM(CASE WHEN activity_type='skill' THEN 1 ELSE 0 END) skill_uses,
                    COUNT(DISTINCT CASE WHEN activity_type='skill' AND skill_name<>'' THEN skill_name END) distinct_skills,
                    COALESCE(SUM(lines_changed),0) patch_lines_changed
                 FROM activities{act_where}""",
            act_values,
        ).fetchone())

        tool_activity = rows_as_dicts(conn.execute(
            f"""SELECT date,COALESCE(NULLIF(tool_category,''),'Other') name,COUNT(*) value
                  FROM activities{act_where}
                 {"AND" if act_where else "WHERE"} activity_type='tool'
                 GROUP BY date,name ORDER BY date,name""",
            act_values,
        ).fetchall())
        skill_activity = rows_as_dicts(conn.execute(
            f"""SELECT date,COALESCE(NULLIF(skill_name,''),'Unknown') name,COUNT(*) value
                  FROM activities{act_where}
                 {"AND" if act_where else "WHERE"} activity_type='skill'
                 GROUP BY date,name ORDER BY date,name""",
            act_values,
        ).fetchall())
        patch_daily = rows_as_dicts(conn.execute(
            f"""SELECT date,COALESCE(SUM(lines_changed),0) value
                  FROM activities{act_where}
                 GROUP BY date ORDER BY date""",
            act_values,
        ).fetchall())

        # Aggregate session metrics server-side; the browser receives one row per session.
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
                       SUM(api_equivalent_cost_usd) api_cost,
                       SUM(CASE WHEN credit_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END) credit_priced,
                       SUM(CASE WHEN api_cost_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END) api_priced
                  FROM responses{where}
                 GROUP BY session_id""",
            values,
        ).fetchall())
        model_by_session = rows_as_dicts(conn.execute(
            f"""SELECT session_id,COALESCE(NULLIF(model,''),'Unknown') model,SUM(total_tokens) total
                  FROM responses{where}
                 GROUP BY session_id,model""",
            values,
        ).fetchall())
        top_models: dict[str, tuple[str, float]] = {}
        for row in model_by_session:
            sid = str(row["session_id"] or "")
            total = float(row["total"] or 0)
            if sid not in top_models or total > top_models[sid][1]:
                top_models[sid] = (str(row["model"]), total)

        turn_by_session = {
            str(row["session_id"] or ""): dict(row)
            for row in conn.execute(
                f"""SELECT session_id,COUNT(*) turns,COALESCE(SUM(duration_ms),0) duration,
                           SUM(CASE WHEN status IN ('failed','aborted') THEN 1 ELSE 0 END) failures,
                           COALESCE(MAX(context_utilization_pct),0) max_context
                      FROM turns{turn_where}
                     GROUP BY session_id""",
                turn_values,
            ).fetchall()
        }
        sessions: list[dict[str, object]] = []
        for row in session_rows:
            sid = str(row.get("session_id") or "")
            tr = turn_by_session.get(sid, {})
            total = float(row.get("total") or 0)
            sessions.append({
                **row,
                "name": row.get("name") or sid or "Unknown session",
                "top_model": top_models.get(sid, ("Unknown", 0))[0],
                "turns": int(tr.get("turns") or 0),
                "duration": float(tr.get("duration") or 0),
                "failures": int(tr.get("failures") or 0),
                "max_context": float(tr.get("max_context") or 0),
                "credit_coverage": float(row.get("credit_priced") or 0) / total * 100.0 if total else 0.0,
                "api_coverage": float(row.get("api_priced") or 0) / total * 100.0 if total else 0.0,
            })

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

        limit_where, limit_values = where_for({k: v for k, v in filters.items() if k in {"from", "to", "model"}})
        latest_limit = conn.execute(
            f"""SELECT * FROM rate_limits{limit_where} ORDER BY timestamp_utc DESC LIMIT 1""",
            limit_values,
        ).fetchone()

        metadata = decode_metadata(conn)
        return {
            "ready": True,
            "filters": filters,
            "metadata": metadata,
            "summary": summary,
            "turn_summary": turn_summary,
            "activity_summary": activity_summary,
            "models": models,
            "agents": agents,
            "efforts": efforts,
            "agent_types": agent_types,
            "daily_models": daily_models,
            "turns_by_model": turns_by_model,
            "tool_activity": tool_activity,
            "skill_activity": skill_activity,
            "patch_daily": patch_daily,
            "sessions": sessions,
            "configured_agents": configured,
            "latest_rate_limit": dict(latest_limit) if latest_limit else None,
        }
    finally:
        conn.close()


def refresh_status() -> dict[str, object]:
    with _REFRESH_LOCK:
        return json.loads(json.dumps(_REFRESH))


def start_refresh(data_dir: Path, codex_home: Path, request: dict[str, object]) -> tuple[bool, dict[str, object]]:
    with _REFRESH_LOCK:
        if _REFRESH.get("status") == "running":
            return False, dict(_REFRESH)
        job_id = str(uuid.uuid4())
        _REFRESH.update({
            "id": job_id,
            "status": "running",
            "started_at": utc_now(),
            "finished_at": None,
            "request": request,
            "message": "Refreshing Codex telemetry…",
            "log": [],
            "return_code": None,
        })

    def worker() -> None:
        cmd = [
            sys.executable,
            str(ROOT / "collect_codex_usage.py"),
            "--codex-home", str(codex_home),
            "--output-dir", str(data_dir),
        ]
        if request.get("mode") == "range":
            cmd += ["--from-date", str(request["from"]), "--to-date", str(request["to"])]
        else:
            cmd += ["--days", str(int(request.get("days") or 7))]
        if request.get("scan_all"):
            cmd.append("--scan-all")
        try:
            proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=None)
            log_lines = ((proc.stdout or "") + "
" + (proc.stderr or "")).strip().splitlines()[-40:]
            with _REFRESH_LOCK:
                _REFRESH["return_code"] = proc.returncode
                _REFRESH["log"] = log_lines
                _REFRESH["finished_at"] = utc_now()
                if proc.returncode == 0:
                    _REFRESH["status"] = "completed"
                    _REFRESH["message"] = "Refresh completed."
                else:
                    _REFRESH["status"] = "failed"
                    _REFRESH["message"] = f"Refresh failed with exit code {proc.returncode}."
        except Exception as exc:
            with _REFRESH_LOCK:
                _REFRESH["status"] = "failed"
                _REFRESH["finished_at"] = utc_now()
                _REFRESH["message"] = str(exc)
                _REFRESH["log"] = traceback.format_exc().splitlines()[-40:]

    threading.Thread(target=worker, daemon=True, name=f"refresh-{job_id[:8]}").start()
    return True, refresh_status()


def ensure_database(data_dir: Path, codex_home: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    if db_path(data_dir).exists():
        return
    if not (data_dir / "codex_usage_records.csv").exists() or not codex_home.exists():
        return
    # One-day refresh retains historical CSV rows and creates the initial DB snapshot.
    subprocess.run(
        [
            sys.executable, str(ROOT / "collect_codex_usage.py"),
            "--days", "1",
            "--codex-home", str(codex_home),
            "--output-dir", str(data_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "CodexUsageDashboard/1.0"

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    @property
    def app_server(self):
        return self.server

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s
" % (self.address_string(), fmt % args))

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
                filters = parse_filters(parse_qs(parsed.query))
                self.send_json(dashboard_payload(self.app_server.data_dir, filters))
            except (ValueError, sqlite3.Error) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
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
    ensure_database(data_dir, codex_home)
    server = DashboardServer((args.host, args.port), DashboardHandler, data_dir, codex_home)
    print(f"Codex usage dashboard: http://127.0.0.1:{args.port}/")
    print(f"Codex source (read-only mount recommended): {codex_home}")
    print(f"Analytics database: {db_path(data_dir)}")
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
