#!/usr/bin/env python3
"""Collect local Codex rollout telemetry into idempotent local datasets.

Reads current Codex CLI/Desktop rollouts under ~/.codex and writes:
  data/codex_usage_records.csv  - one row per model response/token record
  data/codex_turns.csv          - one row per task/turn lifecycle
  data/codex_activity.csv       - tool/plugin/skill activity (no raw arguments)
  data/codex_rate_limits.csv    - observed primary/secondary usage pressure
  data/codex_usage_daily.csv    - daily token/credit summary
  data/codex_agents.csv         - configured ~/.codex/agents inventory
  data/codex_usage_data.js      - compact browser bundle for the local dashboard

The last N *local calendar days* are rebuilt, not appended. Existing rows for
those days are removed and regenerated from rollouts, so reruns are idempotent.

No third-party packages are required. The collector intentionally does not save
prompt text, assistant text, tool arguments, or tool outputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Current ChatGPT Business / Codex token-based credit rate card, 23 Sep 2026.
# Rates are credits per 1M *uncached input*, cached input, and output tokens.
# GPT-5.6 Sol promotional purchased-credit pricing is reflected here.
# Source: https://help.openai.com/en/articles/11481834-cha
CREDIT_RATES: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (250.0, 25.0, 1250.0),
    "gpt-5.6": (100.0, 10.0, 500.0),
    "gpt-5.6-sol": (100.0, 10.0, 500.0),
    "gpt-5.6-terra": (50.0, 5.0, 300.0),
    "gpt-5.6-luna": (5.0, 0.5, 30.0),
    "gpt-5.5": (125.0, 12.5, 750.0),
    "gpt-5.5-cyber": (312.5, 31.25, 1875.0),
    "gpt-5.4": (62.5, 6.25, 375.0),
    "gpt-5.4-mini": (18.75, 1.875, 113.0),
    "gpt-5.3-codex": (43.75, 4.375, 350.0),
    "gpt-5.2": (43.75, 4.375, 350.0),
    "daybreak-blue": (100.0, 10.0, 500.0),
    "gpt-daybreak-blue-latest": (100.0, 10.0, 500.0),
    "daybreak-red": (312.5, 31.25, 1875.0),
    "gpt-daybreak-red-latest": (312.5, 31.25, 1875.0),
    "gpt-rosalind-research": (125.0, 12.5, 625.0),
    # OpenAI's current Business rate card states Auto review uses GPT-5.4.
    "codex-auto-review": (62.5, 6.25, 375.0),
}
CREDIT_RATE_AS_OF = "2026-09-23"
CREDIT_RATE_SOURCE = "https://help.openai.com/en/articles/11481834-cha"

# Fast/priority service-tier multiplier where current OpenAI documentation gives
# a deterministic public premium. Unknown fast rates are deliberately not guessed.
FAST_MULTIPLIERS = {
    "gpt-6-astra": 2.5,
    "gpt-5.6": 2.0,
    "gpt-5.6-sol": 2.0,
    "gpt-5.6-terra": 2.0,
    "gpt-5.6-luna": 2.0,
}

RECORD_FIELDS = [
    "date", "timestamp_utc", "timestamp_local",
    "session_id", "session_name", "thread_id", "thread_name",
    "thread_start_utc", "thread_start_local", "thread_latest_utc", "thread_latest_local",
    "parent_thread_id", "thread_source", "session_source", "originator", "cli_version",
    "agent_type", "agent_role", "agent_nickname", "agent_label", "project",
    "model", "reasoning_effort", "service_tier", "turn_id", "root_turn_id", "response_id",
    "is_compaction", "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "fresh_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
    "cache_hit_pct", "estimated_credits", "credit_rate_status", "source_rollout",
]
RECORD_INT_FIELDS = {
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "fresh_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
}
RECORD_FLOAT_FIELDS = {"cache_hit_pct", "estimated_credits"}

TURN_FIELDS = [
    "date", "started_utc", "started_local", "completed_utc", "completed_local",
    "session_id", "session_name", "thread_id", "thread_name", "parent_thread_id",
    "agent_type", "agent_role", "agent_nickname", "agent_label", "project",
    "turn_id", "model", "reasoning_effort", "service_tier", "status", "error_type",
    "duration_ms", "time_to_first_token_ms", "model_context_window",
    "responses", "input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "estimated_credits", "credit_coverage_pct",
    "compaction_responses", "max_request_input_tokens", "context_utilization_pct", "source_rollout",
]
TURN_INT_FIELDS = {
    "duration_ms", "time_to_first_token_ms", "model_context_window", "responses",
    "input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "compaction_responses", "max_request_input_tokens",
}
TURN_FLOAT_FIELDS = {"estimated_credits", "credit_coverage_pct", "context_utilization_pct"}

ACTIVITY_FIELDS = [
    "date", "timestamp_utc", "timestamp_local", "session_id", "session_name", "thread_id",
    "thread_name", "turn_id", "project", "agent_type", "agent_role", "agent_label",
    "model", "reasoning_effort", "service_tier", "activity_type", "tool_name", "tool_category",
    "plugin_name", "skill_name", "call_id", "status", "duration_ms", "lines_added",
    "lines_deleted", "lines_changed", "source_rollout",
]
ACTIVITY_INT_FIELDS = {"duration_ms", "lines_added", "lines_deleted", "lines_changed"}

LIMIT_FIELDS = [
    "date", "timestamp_utc", "timestamp_local", "session_id", "thread_id", "turn_id", "model",
    "primary_used_pct", "primary_window_minutes", "primary_resets_at",
    "secondary_used_pct", "secondary_window_minutes", "secondary_resets_at",
    "plan_type", "rate_limit_reached_type", "source_rollout",
]
LIMIT_INT_FIELDS = {"primary_window_minutes", "primary_resets_at", "secondary_window_minutes", "secondary_resets_at"}
LIMIT_FLOAT_FIELDS = {"primary_used_pct", "secondary_used_pct"}

AGENT_FIELDS = ["name", "description", "model", "reasoning_effort", "sandbox_mode", "config_file"]

SKILL_URI_RE = re.compile(r"skills://([^\s\"'`)]+?)/(?:skill|SKILL)\.md", re.IGNORECASE)
MCP_CALL_RE = re.compile(r"(?:tools\.)?mcp__([A-Za-z0-9_]+)__([A-Za-z0-9_]+)")


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    p = argparse.ArgumentParser(description="Extract Codex usage for the last N local calendar days.")
    p.add_argument("--days", type=int, required=True, help="Local calendar days to rebuild, including today.")
    p.add_argument("--codex-home", type=Path, default=default_home, help=f"Codex data directory (default: {default_home}).")
    p.add_argument("--output-dir", type=Path, default=here / "data", help="Output directory (default: ./data).")
    p.add_argument("--scan-all", action="store_true", help="Scan every rollout rather than narrowing by mtime.")
    p.add_argument("--verbose", action="store_true", help="Print malformed/skipped details.")
    return p.parse_args()


def parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    value = ts.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_epoch(value: Any) -> datetime | None:
    try:
        if value is None or value == "":
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def iso_utc(dt: datetime | None) -> str:
    return dt.isoformat().replace("+00:00", "Z") if dt else ""


def to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def model_key(model: Any) -> str:
    return str(model or "").strip().lower().replace("_", "-")


def estimate_credits(model: str, fresh: int, cached: int, output: int, service_tier: str) -> tuple[float, str]:
    key = model_key(model)
    rates = CREDIT_RATES.get(key)
    if not rates:
        return 0.0, "unpriced_model"
    mult = 1.0
    tier = str(service_tier or "").strip().lower()
    if tier in {"priority", "fast"}:
        if key not in FAST_MULTIPLIERS:
            return 0.0, "unpriced_fast_tier"
        mult = FAST_MULTIPLIERS[key]
    inp, cache, out = rates
    credits = ((fresh / 1_000_000) * inp + (cached / 1_000_000) * cache + (output / 1_000_000) * out) * mult
    status = "priced_fast" if mult != 1.0 else ("priced_standard" if tier in {"default", "standard"} else "priced_standard_assumed")
    return credits, status


def load_thread_names(codex_home: Path, verbose: bool = False) -> dict[str, str]:
    path = codex_home / "session_index.jsonl"
    names: dict[str, str] = {}
    if not path.exists():
        return names
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                thread_id = row.get("id")
                thread_name = row.get("thread_name")
                if thread_id and isinstance(thread_name, str) and thread_name.strip():
                    names[str(thread_id)] = thread_name.strip()
    except OSError as exc:
        if verbose:
            print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
    return names


def load_configured_agents(codex_home: Path, verbose: bool = False) -> list[dict[str, str]]:
    agents_dir = codex_home / "agents"
    if not agents_dir.exists():
        return []
    try:
        import tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        tomllib = None
    wanted = {"name", "description", "model", "model_reasoning_effort", "reasoning_effort", "sandbox_mode"}
    agents: list[dict[str, str]] = []
    for path in sorted(agents_dir.glob("*.toml")):
        data: dict[str, Any] = {}
        try:
            if tomllib is not None:
                with path.open("rb") as f:
                    parsed = tomllib.load(f)
                if isinstance(parsed, dict):
                    data = parsed
            else:
                for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if key not in wanted:
                        continue
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                        value = value[1:-1]
                    data[key] = value
        except (OSError, ValueError) as exc:
            if verbose:
                print(f"Warning: could not parse agent config {path}: {exc}", file=sys.stderr)

        def text_value(key: str) -> str:
            value = data.get(key)
            return value.strip() if isinstance(value, str) else ""

        agents.append({
            "name": text_value("name") or path.stem,
            "description": text_value("description"),
            "model": text_value("model"),
            "reasoning_effort": text_value("model_reasoning_effort") or text_value("reasoning_effort"),
            "sandbox_mode": text_value("sandbox_mode"),
            "config_file": path.name,
        })
    return agents


def discover_rollouts(codex_home: Path, earliest_local: datetime, scan_all: bool) -> list[Path]:
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    cutoff_epoch = earliest_local.timestamp() - 3600
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            if scan_all:
                paths.append(path)
                continue
            try:
                if path.stat().st_mtime >= cutoff_epoch:
                    paths.append(path)
            except OSError:
                paths.append(path)
    return sorted(set(paths))


def dig_first(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v)
        for v in obj.values():
            found = dig_first(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = dig_first(v, keys)
            if found:
                return found
    return ""


def normalize_session_meta(payload: dict[str, Any]) -> dict[str, str]:
    def s(key: str) -> str:
        value = payload.get(key)
        return "" if value is None or isinstance(value, (dict, list)) else str(value)

    cwd = s("cwd")
    project = ""
    if cwd:
        cleaned = cwd.rstrip("/\\")
        project = cleaned.replace("\\", "/").split("/")[-1]

    source = payload.get("source")
    nested_role = dig_first(source, {"agent_role", "role"}) if isinstance(source, (dict, list)) else ""
    nested_nick = dig_first(source, {"agent_nickname", "nickname"}) if isinstance(source, (dict, list)) else ""
    thread_source = s("thread_source")
    if not thread_source and isinstance(source, dict) and "subagent" in source:
        thread_source = "subagent"
    session_source = s("source") or ("structured" if isinstance(source, (dict, list)) else "")

    return {
        "meta_thread_id": s("id") or s("thread_id"),
        "session_id": s("session_id"),
        "parent_thread_id": s("parent_thread_id"),
        "thread_source": thread_source,
        "session_source": session_source,
        "originator": s("originator"),
        "cli_version": s("cli_version"),
        "agent_role": s("agent_role") or nested_role,
        "agent_nickname": s("agent_nickname") or nested_nick,
        "project": project,
    }


def classify_agent(meta: dict[str, str]) -> tuple[str, str]:
    thread_source = (meta.get("thread_source") or "").strip()
    src = thread_source.lower()
    has_parent = bool(meta.get("parent_thread_id"))
    has_agent = bool(meta.get("agent_role") or meta.get("agent_nickname"))
    is_sub = src in {"subagent", "guardian", "reviewer", "agent"} or has_parent or has_agent
    if not is_sub:
        return "Main", "Main"
    role = (meta.get("agent_role") or "").strip()
    return "Subagent", role or thread_source or "Subagent"


def read_jsonl(path: Path, verbose: bool) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    if verbose:
                        print(f"Warning: malformed JSON {path}:{line_no}", file=sys.stderr)
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError as exc:
        if verbose:
            print(f"Warning: could not read {path}: {exc}", file=sys.stderr)


def source_rel(path: Path, codex_home: Path) -> str:
    try:
        return str(path.relative_to(codex_home))
    except ValueError:
        return str(path)


def call_payload_text(payload: dict[str, Any]) -> str:
    for key in ("input", "arguments", "args"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except TypeError:
                pass
    return ""


def call_diff_text(payload: dict[str, Any]) -> str:
    raw = call_payload_text(payload)
    # Function-call arguments often wrap the patch in a JSON property.
    if payload.get("type") == "function_call" and isinstance(payload.get("arguments"), str):
        try:
            args = json.loads(payload["arguments"])
            if isinstance(args, dict):
                for key in ("command", "patch", "input", "diff"):
                    if isinstance(args.get(key), str):
                        return args[key]
        except json.JSONDecodeError:
            pass
    return raw


def count_diff_lines(text: str) -> tuple[int, int]:
    added = deleted = 0
    for line in str(text or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def tool_category(name: str) -> tuple[str, str]:
    raw = str(name or "").strip()
    low = raw.lower()
    plugin = ""
    if low.startswith("mcp__"):
        parts = raw.split("__")
        if len(parts) >= 3:
            plugin = parts[1]
            return plugin.replace("_", " "), plugin
    if any(x in low for x in ("apply_patch", "patch_apply", "file_change")):
        return "Code changes", plugin
    if low in {"exec", "exec_command", "shell", "local_shell", "write_stdin", "js", "js_reset", "unified_exec"} or "command" in low:
        return "Local compute", plugin
    if "skill" in low:
        return "Skills", plugin
    if "github" in low:
        return "GitHub", "GitHub"
    if "site" in low:
        return "Sites", "Sites"
    if "web" in low or "search" in low:
        return "Web", plugin
    if "image" in low:
        return "Image", plugin
    return "Other", plugin


def outcome_from_payload(payload: dict[str, Any], output_text: str = "") -> str:
    success = payload.get("success")
    if isinstance(success, bool):
        return "success" if success else "failed"
    exit_code = payload.get("exit_code")
    if exit_code is not None:
        return "success" if to_int(exit_code) == 0 else "failed"
    status = str(payload.get("status") or "").lower()
    if status in {"completed", "success", "succeeded", "ok"}:
        return "success"
    if status in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    text = output_text.lower()
    if "exit code: 0" in text or "success." in text or text.startswith("success"):
        return "success"
    if "failed" in text or "error:" in text or "exit code: 1" in text:
        return "failed"
    return "unknown"


def parse_rollout(
    path: Path,
    selected_dates: set[str],
    local_tz,
    thread_names: dict[str, str],
    codex_home: Path,
    verbose: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    items = list(read_jsonl(path, verbose))
    if not items:
        return [], [], [], []

    item_times = [dt for item in items if (dt := parse_iso(item.get("timestamp"))) is not None]
    thread_start_utc = min(item_times) if item_times else None
    thread_latest_utc = max(item_times) if item_times else None

    meta: dict[str, str] = {
        "meta_thread_id": "", "session_id": "", "parent_thread_id": "", "thread_source": "",
        "session_source": "", "originator": "", "cli_version": "", "agent_role": "",
        "agent_nickname": "", "project": "",
    }
    for item in items:
        if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
            meta.update({k: v for k, v in normalize_session_meta(item["payload"]).items() if v})

    thread_id_default = meta.get("meta_thread_id", "")
    session_id_default = meta.get("session_id", "") or (meta.get("parent_thread_id") or thread_id_default)
    thread_name_default = thread_names.get(thread_id_default, "")
    session_name_default = thread_names.get(session_id_default, "") or (thread_name_default if session_id_default == thread_id_default else "")
    agent_type, agent_label = classify_agent(meta)
    rel = source_rel(path, codex_home)

    # Chronological context snapshot by turn.
    turn_context: dict[str, dict[str, str]] = {}
    current_ctx = {"model": "", "effort": "", "service_tier": ""}
    active_turn_id = ""
    compaction_response_ids: set[str] = set()

    # Tool call lifecycle staging.
    calls: dict[str, dict[str, Any]] = {}
    call_order: list[str] = []
    skill_events: list[dict[str, Any]] = []
    plugin_events: list[dict[str, Any]] = []

    # Turn lifecycle staging.
    turns: dict[str, dict[str, Any]] = {}
    limits: list[dict[str, Any]] = []

    for item in items:
        typ = item.get("type")
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        ts_utc = parse_iso(item.get("timestamp"))

        if typ == "thread_settings_applied":
            if payload.get("model"):
                current_ctx["model"] = str(payload.get("model"))
            if payload.get("reasoning_effort"):
                current_ctx["effort"] = str(payload.get("reasoning_effort"))
            if payload.get("service_tier") is not None:
                current_ctx["service_tier"] = str(payload.get("service_tier") or "")
            continue

        if typ == "turn_context":
            active_turn_id = str(payload.get("turn_id") or active_turn_id)
            ctx = {
                "model": str(payload.get("model") or current_ctx.get("model") or ""),
                "effort": str(payload.get("effort") or payload.get("reasoning_effort") or current_ctx.get("effort") or ""),
                "service_tier": str(payload.get("service_tier") or current_ctx.get("service_tier") or ""),
            }
            current_ctx.update({k: v for k, v in ctx.items() if v})
            if active_turn_id:
                turn_context[active_turn_id] = dict(current_ctx)
                t = turns.setdefault(active_turn_id, {"turn_id": active_turn_id})
                t.update({k: v for k, v in current_ctx.items() if v})
            continue

        if typ == "compacted":
            response_id = payload.get("compaction_response_id")
            if response_id:
                compaction_response_ids.add(str(response_id))
            continue

        if typ == "event_msg":
            et = str(payload.get("type") or "")
            if et in {"task_started", "turn_started"}:
                active_turn_id = str(payload.get("turn_id") or active_turn_id)
                if active_turn_id:
                    t = turns.setdefault(active_turn_id, {"turn_id": active_turn_id})
                    start = parse_epoch(payload.get("started_at")) or ts_utc
                    if start:
                        t["started_utc"] = start
                    t["status"] = t.get("status") or "started"
                    t["model_context_window"] = to_int(payload.get("model_context_window"))
                    if active_turn_id in turn_context:
                        t.update(turn_context[active_turn_id])
                continue
            if et in {"task_complete", "turn_complete"}:
                tid = str(payload.get("turn_id") or active_turn_id)
                if tid:
                    t = turns.setdefault(tid, {"turn_id": tid})
                    start = parse_epoch(payload.get("started_at")) or t.get("started_utc")
                    end = parse_epoch(payload.get("completed_at")) or ts_utc
                    if start:
                        t["started_utc"] = start
                    if end:
                        t["completed_utc"] = end
                    t["duration_ms"] = to_int(payload.get("duration_ms"))
                    t["time_to_first_token_ms"] = to_int(payload.get("time_to_first_token_ms"))
                    err = payload.get("error")
                    if err:
                        t["status"] = "failed"
                        if isinstance(err, dict):
                            t["error_type"] = str(err.get("codex_error_info") or err.get("type") or "error")
                        else:
                            t["error_type"] = "error"
                    else:
                        t["status"] = "completed"
                    if tid in turn_context:
                        t.update(turn_context[tid])
                continue
            if et in {"turn_aborted", "task_aborted"}:
                tid = str(payload.get("turn_id") or active_turn_id)
                if tid:
                    t = turns.setdefault(tid, {"turn_id": tid})
                    start = parse_epoch(payload.get("started_at")) or t.get("started_utc")
                    end = parse_epoch(payload.get("completed_at")) or ts_utc
                    if start:
                        t["started_utc"] = start
                    if end:
                        t["completed_utc"] = end
                    t["duration_ms"] = to_int(payload.get("duration_ms"))
                    t["status"] = "aborted"
                    t["error_type"] = str(payload.get("reason") or "aborted")
                    if tid in turn_context:
                        t.update(turn_context[tid])
                continue
            if et == "token_count":
                rate = payload.get("rate_limits") or {}
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    win = to_int(info.get("model_context_window"))
                    if win and active_turn_id:
                        turns.setdefault(active_turn_id, {"turn_id": active_turn_id})["model_context_window"] = win
                if isinstance(rate, dict) and ts_utc:
                    p = rate.get("primary") or {}
                    s = rate.get("secondary") or {}
                    if isinstance(p, dict) or isinstance(s, dict):
                        local = ts_utc.astimezone(local_tz)
                        if local.date().isoformat() in selected_dates:
                            limits.append({
                                "date": local.date().isoformat(), "timestamp_utc": iso_utc(ts_utc), "timestamp_local": local.isoformat(),
                                "session_id": session_id_default, "thread_id": thread_id_default, "turn_id": active_turn_id,
                                "model": turn_context.get(active_turn_id, current_ctx).get("model", ""),
                                "primary_used_pct": to_float(p.get("used_percent")) if isinstance(p, dict) else 0.0,
                                "primary_window_minutes": to_int(p.get("window_minutes")) if isinstance(p, dict) else 0,
                                "primary_resets_at": to_int(p.get("resets_at")) if isinstance(p, dict) else 0,
                                "secondary_used_pct": to_float(s.get("used_percent")) if isinstance(s, dict) else 0.0,
                                "secondary_window_minutes": to_int(s.get("window_minutes")) if isinstance(s, dict) else 0,
                                "secondary_resets_at": to_int(s.get("resets_at")) if isinstance(s, dict) else 0,
                                "plan_type": str(rate.get("plan_type") or ""),
                                "rate_limit_reached_type": str(rate.get("rate_limit_reached_type") or ""),
                                "source_rollout": rel,
                            })
                continue
            # Authoritative tool outcomes in event messages.
            if et.endswith("_end"):
                cid = str(payload.get("call_id") or "")
                if cid and cid in calls:
                    c = calls[cid]
                    c["end_utc"] = ts_utc or c.get("end_utc")
                    c["status"] = outcome_from_payload(payload)
                    if et == "patch_apply_end" and isinstance(payload.get("changes"), dict):
                        added = deleted = 0
                        for change in payload["changes"].values():
                            if isinstance(change, dict):
                                a, d = count_diff_lines(str(change.get("unified_diff") or ""))
                                added += a; deleted += d
                        if added or deleted:
                            c["lines_added"] = added; c["lines_deleted"] = deleted
                continue

        if typ == "response_item":
            rt = str(payload.get("type") or "")
            metadata = payload.get("metadata") or {}
            item_turn = str(payload.get("turn_id") or (metadata.get("turn_id") if isinstance(metadata, dict) else "") or active_turn_id)
            if item_turn:
                active_turn_id = item_turn
            if rt in {"function_call", "custom_tool_call", "mcp_call", "local_shell_call"}:
                cid = str(payload.get("call_id") or payload.get("id") or f"anon:{len(call_order)}")
                name = str(payload.get("name") or payload.get("tool_name") or payload.get("server") or rt)
                text = call_payload_text(payload)
                cat, plugin = tool_category(name)
                ctx = turn_context.get(item_turn, current_ctx)
                c = {
                    "call_id": cid, "name": name, "category": cat, "plugin": plugin,
                    "start_utc": ts_utc, "end_utc": None, "turn_id": item_turn,
                    "model": ctx.get("model", ""), "effort": ctx.get("effort", ""), "service_tier": ctx.get("service_tier", ""),
                    "status": "unknown", "lines_added": 0, "lines_deleted": 0,
                }
                if "patch" in name.lower():
                    a, d = count_diff_lines(call_diff_text(payload)); c["lines_added"] = a; c["lines_deleted"] = d
                calls[cid] = c
                call_order.append(cid)

                # Recover plugin/skill use hidden inside functions.exec / code-mode orchestration.
                for m in MCP_CALL_RE.finditer(text):
                    plugin_events.append({"call_id": cid, "plugin": m.group(1).replace("_", " "), "tool": m.group(2), "turn_id": item_turn, "timestamp": ts_utc, "ctx": dict(ctx)})
                seen_skills: set[str] = set()
                for m in SKILL_URI_RE.finditer(text):
                    uri_path = m.group(1).strip("/")
                    parts = [p for p in uri_path.split("/") if p]
                    skill = parts[-1] if parts else uri_path
                    if skill and skill not in seen_skills:
                        seen_skills.add(skill)
                        skill_events.append({"call_id": cid, "skill": skill, "turn_id": item_turn, "timestamp": ts_utc, "ctx": dict(ctx)})
                continue
            if rt in {"function_call_output", "custom_tool_call_output", "mcp_call_output", "local_shell_call_output"}:
                cid = str(payload.get("call_id") or "")
                if cid and cid in calls:
                    c = calls[cid]
                    c["end_utc"] = ts_utc
                    out = payload.get("output")
                    out_text = out if isinstance(out, str) else ""
                    c["status"] = outcome_from_payload(payload, out_text)
                continue

    # Build per-response token records after turn/model context is known.
    records: list[dict[str, Any]] = []
    active_turn_id = ""
    current_ctx = {"model": "", "effort": "", "service_tier": ""}
    for item in items:
        typ = item.get("type")
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if typ == "thread_settings_applied":
            if payload.get("model"): current_ctx["model"] = str(payload.get("model"))
            if payload.get("reasoning_effort"): current_ctx["effort"] = str(payload.get("reasoning_effort"))
            if payload.get("service_tier") is not None: current_ctx["service_tier"] = str(payload.get("service_tier") or "")
        elif typ == "turn_context":
            active_turn_id = str(payload.get("turn_id") or active_turn_id)
            if active_turn_id in turn_context:
                current_ctx.update(turn_context[active_turn_id])
        elif typ == "event_msg" and str(payload.get("type") or "") in {"task_started", "turn_started"}:
            active_turn_id = str(payload.get("turn_id") or active_turn_id)
        elif typ == "token_usage_record":
            usage = payload.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            ts_utc = parse_iso(item.get("timestamp"))
            if not ts_utc:
                continue
            ts_local = ts_utc.astimezone(local_tz)
            date_key = ts_local.date().isoformat()
            if date_key not in selected_dates:
                continue
            tid = str(payload.get("turn_id") or active_turn_id)
            ctx = turn_context.get(tid, current_ctx)
            thread_id = str(payload.get("thread_id") or thread_id_default)
            session_id = str(payload.get("session_id") or session_id_default or thread_id)
            response_id = str(payload.get("response_id") or "")
            if not thread_id or not response_id:
                continue
            inp = to_int(usage.get("input_tokens"))
            cached = to_int(usage.get("cached_input_tokens"))
            cache_write = to_int(usage.get("cache_write_input_tokens", usage.get("cache_creation_input_tokens", 0)))
            out = to_int(usage.get("output_tokens"))
            reasoning = to_int(usage.get("reasoning_output_tokens"))
            total = to_int(usage.get("total_tokens")) or inp + out
            fresh = max(0, inp - cached)
            credits, rate_status = estimate_credits(ctx.get("model", ""), fresh, cached, out, ctx.get("service_tier", ""))
            records.append({
                "date": date_key, "timestamp_utc": iso_utc(ts_utc), "timestamp_local": ts_local.isoformat(),
                "session_id": session_id, "session_name": thread_names.get(session_id, "") or session_name_default,
                "thread_id": thread_id, "thread_name": thread_names.get(thread_id, "") or thread_name_default,
                "thread_start_utc": iso_utc(thread_start_utc), "thread_start_local": thread_start_utc.astimezone(local_tz).isoformat() if thread_start_utc else "",
                "thread_latest_utc": iso_utc(thread_latest_utc), "thread_latest_local": thread_latest_utc.astimezone(local_tz).isoformat() if thread_latest_utc else "",
                "parent_thread_id": meta.get("parent_thread_id", ""), "thread_source": meta.get("thread_source", ""),
                "session_source": meta.get("session_source", ""), "originator": meta.get("originator", ""), "cli_version": meta.get("cli_version", ""),
                "agent_type": agent_type, "agent_role": meta.get("agent_role", ""), "agent_nickname": meta.get("agent_nickname", ""), "agent_label": agent_label,
                "project": meta.get("project", ""), "model": ctx.get("model", ""), "reasoning_effort": ctx.get("effort", ""), "service_tier": ctx.get("service_tier", ""),
                "turn_id": tid, "root_turn_id": str(payload.get("root_turn_id") or ""), "response_id": response_id,
                "is_compaction": "true" if response_id in compaction_response_ids else "false",
                "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": cache_write, "fresh_input_tokens": fresh,
                "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": total,
                "cache_hit_pct": round((cached / inp * 100.0) if inp else 0.0, 4),
                "estimated_credits": round(credits, 6), "credit_rate_status": rate_status, "source_rollout": rel,
            })

    # Materialize turn rows, including turns without token usage.
    turn_rows: list[dict[str, Any]] = []
    for tid, t in turns.items():
        start: datetime | None = t.get("started_utc") if isinstance(t.get("started_utc"), datetime) else None
        end: datetime | None = t.get("completed_utc") if isinstance(t.get("completed_utc"), datetime) else None
        anchor = start or end
        if not anchor:
            continue
        local_anchor = anchor.astimezone(local_tz)
        if local_anchor.date().isoformat() not in selected_dates:
            continue
        ctx = turn_context.get(tid, {"model": t.get("model", ""), "effort": t.get("effort", ""), "service_tier": t.get("service_tier", "")})
        turn_rows.append({
            "date": local_anchor.date().isoformat(),
            "started_utc": iso_utc(start), "started_local": start.astimezone(local_tz).isoformat() if start else "",
            "completed_utc": iso_utc(end), "completed_local": end.astimezone(local_tz).isoformat() if end else "",
            "session_id": session_id_default, "session_name": session_name_default,
            "thread_id": thread_id_default, "thread_name": thread_name_default,
            "parent_thread_id": meta.get("parent_thread_id", ""), "agent_type": agent_type,
            "agent_role": meta.get("agent_role", ""), "agent_nickname": meta.get("agent_nickname", ""), "agent_label": agent_label,
            "project": meta.get("project", ""), "turn_id": tid,
            "model": ctx.get("model", "") or str(t.get("model") or ""),
            "reasoning_effort": ctx.get("effort", "") or str(t.get("effort") or ""),
            "service_tier": ctx.get("service_tier", "") or str(t.get("service_tier") or ""),
            "status": str(t.get("status") or "incomplete"), "error_type": str(t.get("error_type") or ""),
            "duration_ms": to_int(t.get("duration_ms")), "time_to_first_token_ms": to_int(t.get("time_to_first_token_ms")),
            "model_context_window": to_int(t.get("model_context_window")),
            "responses": 0, "input_tokens": 0, "cached_input_tokens": 0, "fresh_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "total_tokens": 0, "estimated_credits": 0.0, "credit_coverage_pct": 0.0,
            "compaction_responses": 0, "max_request_input_tokens": 0, "context_utilization_pct": 0.0,
            "source_rollout": rel,
        })

    # Materialize tool calls + synthetic plugin/skill detections.
    activities: list[dict[str, Any]] = []
    base_common = {
        "session_id": session_id_default, "session_name": session_name_default, "thread_id": thread_id_default,
        "thread_name": thread_name_default, "project": meta.get("project", ""), "agent_type": agent_type,
        "agent_role": meta.get("agent_role", ""), "agent_label": agent_label, "source_rollout": rel,
    }
    for cid in call_order:
        c = calls[cid]
        start = c.get("start_utc") if isinstance(c.get("start_utc"), datetime) else None
        if not start:
            continue
        local = start.astimezone(local_tz)
        if local.date().isoformat() not in selected_dates:
            continue
        end = c.get("end_utc") if isinstance(c.get("end_utc"), datetime) else None
        duration = int((end - start).total_seconds() * 1000) if end and end >= start else 0
        status = str(c.get("status") or "unknown")
        added = to_int(c.get("lines_added")); deleted = to_int(c.get("lines_deleted"))
        # Do not count failed patches as code changed.
        if status == "failed":
            added = deleted = 0
        activities.append({
            "date": local.date().isoformat(), "timestamp_utc": iso_utc(start), "timestamp_local": local.isoformat(),
            **base_common, "turn_id": str(c.get("turn_id") or ""), "model": str(c.get("model") or ""),
            "reasoning_effort": str(c.get("effort") or ""), "service_tier": str(c.get("service_tier") or ""),
            "activity_type": "plugin" if str(c.get("plugin") or "") else "tool", "tool_name": str(c.get("name") or ""), "tool_category": str(c.get("category") or "Other"),
            "plugin_name": str(c.get("plugin") or ""), "skill_name": "", "call_id": cid, "status": status,
            "duration_ms": duration, "lines_added": added, "lines_deleted": deleted, "lines_changed": added + deleted,
        })

    for ev in plugin_events:
        start = ev.get("timestamp") if isinstance(ev.get("timestamp"), datetime) else None
        if not start:
            continue
        local = start.astimezone(local_tz)
        if local.date().isoformat() not in selected_dates:
            continue
        ctx = ev.get("ctx") or {}
        activities.append({
            "date": local.date().isoformat(), "timestamp_utc": iso_utc(start), "timestamp_local": local.isoformat(),
            **base_common, "turn_id": str(ev.get("turn_id") or ""), "model": str(ctx.get("model") or ""),
            "reasoning_effort": str(ctx.get("effort") or ""), "service_tier": str(ctx.get("service_tier") or ""),
            "activity_type": "plugin", "tool_name": str(ev.get("tool") or ""), "tool_category": str(ev.get("plugin") or "Plugin"),
            "plugin_name": str(ev.get("plugin") or ""), "skill_name": "", "call_id": str(ev.get("call_id") or ""),
            "status": "observed", "duration_ms": 0, "lines_added": 0, "lines_deleted": 0, "lines_changed": 0,
        })

    for ev in skill_events:
        start = ev.get("timestamp") if isinstance(ev.get("timestamp"), datetime) else None
        if not start:
            continue
        local = start.astimezone(local_tz)
        if local.date().isoformat() not in selected_dates:
            continue
        ctx = ev.get("ctx") or {}
        activities.append({
            "date": local.date().isoformat(), "timestamp_utc": iso_utc(start), "timestamp_local": local.isoformat(),
            **base_common, "turn_id": str(ev.get("turn_id") or ""), "model": str(ctx.get("model") or ""),
            "reasoning_effort": str(ctx.get("effort") or ""), "service_tier": str(ctx.get("service_tier") or ""),
            "activity_type": "skill", "tool_name": "skill", "tool_category": "Skills", "plugin_name": "",
            "skill_name": str(ev.get("skill") or ""), "call_id": str(ev.get("call_id") or ""), "status": "observed",
            "duration_ms": 0, "lines_added": 0, "lines_deleted": 0, "lines_changed": 0,
        })

    return records, turn_rows, activities, limits


def load_csv(path: Path, fields: list[str], int_fields: set[str] | None = None, float_fields: set[str] | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    ints = int_fields or set(); floats = float_fields or set()
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                out: dict[str, Any] = {}
                for field in fields:
                    value = row.get(field, "")
                    out[field] = to_int(value) if field in ints else (to_float(value) if field in floats else value)
                rows.append(out)
    except OSError:
        return []
    return rows


def upgrade_record(row: dict[str, Any]) -> dict[str, Any]:
    inp = to_int(row.get("input_tokens")); cached = to_int(row.get("cached_input_tokens")); out = to_int(row.get("output_tokens"))
    fresh = to_int(row.get("fresh_input_tokens")) or max(0, inp - cached)
    row["fresh_input_tokens"] = fresh
    credits, status = estimate_credits(str(row.get("model") or ""), fresh, cached, out, str(row.get("service_tier") or ""))
    row["estimated_credits"] = round(credits, 6)
    row["credit_rate_status"] = status
    return row


def dedupe_records(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("thread_id") or ""), str(row.get("response_id") or ""))
        if not all(key):
            continue
        if key not in by or str(row.get("timestamp_utc") or "") >= str(by[key].get("timestamp_utc") or ""):
            by[key] = upgrade_record(row)
    return sorted(by.values(), key=lambda r: (str(r.get("timestamp_utc") or ""), str(r.get("thread_id") or "")))


def dedupe_turns(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("thread_id") or ""), str(row.get("turn_id") or ""))
        if not all(key):
            continue
        stamp = str(row.get("completed_utc") or row.get("started_utc") or "")
        prev = by.get(key)
        prev_stamp = str(prev.get("completed_utc") or prev.get("started_utc") or "") if prev else ""
        if prev is None or stamp >= prev_stamp:
            by[key] = row
    return sorted(by.values(), key=lambda r: (str(r.get("started_utc") or r.get("completed_utc") or ""), str(r.get("thread_id") or "")))


def dedupe_activity(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("thread_id") or ""), str(row.get("call_id") or ""), str(row.get("activity_type") or ""), str(row.get("skill_name") or row.get("plugin_name") or row.get("tool_name") or ""))
        if not key[0] or not key[1]:
            continue
        by[key] = row
    return sorted(by.values(), key=lambda r: (str(r.get("timestamp_utc") or ""), str(r.get("thread_id") or ""), str(r.get("call_id") or "")))


def dedupe_limits(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("thread_id") or ""), str(row.get("timestamp_utc") or ""))
        if all(key): by[key] = row
    return sorted(by.values(), key=lambda r: str(r.get("timestamp_utc") or ""))


def enrich_turns(turns: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usage: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        key = (str(r.get("thread_id") or ""), str(r.get("turn_id") or ""))
        if all(key): usage[key].append(r)
    for t in turns:
        rs = usage.get((str(t.get("thread_id") or ""), str(t.get("turn_id") or "")), [])
        t["responses"] = len(rs)
        for field in ("input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            t[field] = sum(to_int(r.get(field)) for r in rs)
        priced_tokens = sum(to_int(r.get("total_tokens")) for r in rs if str(r.get("credit_rate_status") or "").startswith("priced"))
        total_tokens = sum(to_int(r.get("total_tokens")) for r in rs)
        t["estimated_credits"] = round(sum(to_float(r.get("estimated_credits")) for r in rs), 6)
        t["credit_coverage_pct"] = round((priced_tokens / total_tokens * 100.0) if total_tokens else 0.0, 4)
        t["compaction_responses"] = sum(1 for r in rs if str(r.get("is_compaction") or "").lower() == "true")
        mx = max((to_int(r.get("input_tokens")) for r in rs), default=0)
        t["max_request_input_tokens"] = mx
        win = to_int(t.get("model_context_window"))
        t["context_utilization_pct"] = round((mx / win * 100.0) if win else 0.0, 4)
    return turns


def write_csv_atomic(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    os.replace(tmp, path)


def daily_summary(records: list[dict[str, Any]], turns: list[dict[str, Any]], activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for r in records:
        day = str(r.get("date") or "")
        if not day: continue
        g = grouped.setdefault(day, {"date":day,"responses":0,"sessions":set(),"threads":set(),"models":set(),"input_tokens":0,"cached_input_tokens":0,"fresh_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":0,"estimated_credits":0.0,"compaction_responses":0,"priced_tokens":0})
        g["responses"] += 1
        if r.get("session_id"): g["sessions"].add(r["session_id"])
        if r.get("thread_id"): g["threads"].add(r["thread_id"])
        if r.get("model"): g["models"].add(r["model"])
        for f in ("input_tokens","cached_input_tokens","fresh_input_tokens","output_tokens","reasoning_output_tokens","total_tokens"):
            g[f] += to_int(r.get(f))
        g["estimated_credits"] += to_float(r.get("estimated_credits"))
        if str(r.get("credit_rate_status") or "").startswith("priced"): g["priced_tokens"] += to_int(r.get("total_tokens"))
        if str(r.get("is_compaction") or "").lower() == "true": g["compaction_responses"] += 1
    turn_counts = defaultdict(int); failures = defaultdict(int); duration = defaultdict(int)
    for t in turns:
        d=str(t.get("date") or ""); turn_counts[d]+=1; duration[d]+=to_int(t.get("duration_ms")); failures[d]+=1 if str(t.get("status") or "") in {"failed","aborted"} else 0
    tool_counts=defaultdict(int); skill_counts=defaultdict(int); code_lines=defaultdict(int)
    for a in activities:
        d=str(a.get("date") or "")
        if a.get("activity_type")=="tool": tool_counts[d]+=1; code_lines[d]+=to_int(a.get("lines_changed"))
        elif a.get("activity_type")=="skill": skill_counts[d]+=1
    out=[]
    for day in sorted(grouped):
        g=grouped[day]; inp=g["input_tokens"]; tot=g["total_tokens"]
        out.append({
            "date":day,"responses":g["responses"],"turns":turn_counts[day],"sessions":len(g["sessions"]),"threads":len(g["threads"]),"models":len(g["models"]),
            "input_tokens":g["input_tokens"],"cached_input_tokens":g["cached_input_tokens"],"fresh_input_tokens":g["fresh_input_tokens"],"output_tokens":g["output_tokens"],"reasoning_output_tokens":g["reasoning_output_tokens"],"total_tokens":tot,
            "cache_hit_pct":round((g["cached_input_tokens"]/inp*100.0) if inp else 0.0,4),"estimated_credits":round(g["estimated_credits"],6),"credit_coverage_pct":round((g["priced_tokens"]/tot*100.0) if tot else 0.0,4),
            "compaction_responses":g["compaction_responses"],"failed_or_aborted_turns":failures[day],"turn_duration_ms":duration[day],"tool_calls":tool_counts[day],"skill_uses":skill_counts[day],"patch_lines_changed":code_lines[day],
        })
    return out


def write_dashboard_data(path: Path, records: list[dict[str, Any]], turns: list[dict[str, Any]], activities: list[dict[str, Any]], limits: list[dict[str, Any]], metadata: dict[str, Any], configured_agents: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metadata":metadata,"records":records,"turns":turns,"activities":activities,"rate_limits":limits,"configured_agents":configured_agents}