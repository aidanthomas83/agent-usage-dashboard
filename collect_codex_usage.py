#!/usr/bin/env python3
"""Collect local Codex rollout telemetry into idempotent local datasets.

Reads current Codex CLI/Desktop rollouts under ~/.codex and writes:
  data/codex_usage_records.csv  - one row per model response/token record
  data/codex_turns.csv          - one row per task/turn lifecycle
  data/codex_activity.csv       - tool/plugin/skill activity (no raw arguments)
  data/codex_rate_limits.csv    - observed primary/secondary usage pressure
  data/codex_usage_daily.csv    - daily token/credit summary
  data/codex_agents.csv         - configured ~/.codex/agents inventory
  data/codex_skills.csv         - configured ~/.codex/skills inventory
  data/codex_usage.sqlite       - SQLite snapshot used by the local dashboard

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
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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

# Counterfactual direct OpenAI API token cost using today's Standard API pricing.
# Prices are USD per 1M ordinary input, cached input, cache-write input, and output tokens.
API_PRICE_AS_OF = "2026-09-23"
API_PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
API_RATES: dict[str, tuple[float, float, float, float]] = {
    "gpt-6-astra": (10.0, 1.0, 12.5, 50.0),
    "gpt-6-sol": (2.0, 0.2, 2.5, 10.0),
    "gpt-6-luna": (0.1, 0.01, 0.125, 0.5),
    "gpt-5.6-sol": (4.0, 0.4, 5.0, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 2.5, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
    "gpt-5.6-cyber": (12.5, 1.25, 15.625, 75.0),
    "gpt-5.5": (5.0, 0.5, 5.0, 30.0),
    "gpt-5.4": (2.5, 0.25, 2.5, 15.0),
    "gpt-5.4-mini": (0.75, 0.075, 0.75, 4.5),
    "gpt-5.3-codex": (1.75, 0.175, 1.75, 14.0),
    "gpt-5.2": (1.75, 0.175, 1.75, 14.0),
}
API_RATE_ALIASES = {
    "gpt-5.6": "gpt-5.6-sol",
    "daybreak-blue": "gpt-5.6-sol",
    "gpt-daybreak-blue-latest": "gpt-5.6-sol",
    "daybreak-red": "gpt-5.6-cyber",
    "gpt-daybreak-red-latest": "gpt-5.6-cyber",
    "codex-auto-review": "gpt-5.4",
}
API_LONG_CONTEXT_MODELS = {
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4",
}
API_LONG_CONTEXT_THRESHOLD = 272_000

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
    "cache_hit_pct", "estimated_credits", "credit_rate_status",
    "api_equivalent_cost_usd", "api_cost_rate_status", "api_long_context",
    "usage_source", "source_rollout",
]
RECORD_INT_FIELDS = {
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "fresh_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
}
RECORD_FLOAT_FIELDS = {"cache_hit_pct", "estimated_credits", "api_equivalent_cost_usd"}

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
SKILL_FIELDS = ["name", "skill_file"]

SKILL_URI_RE = re.compile(r"skills://([^\s\"'`)]+?)/(?:skill|SKILL)\.md", re.IGNORECASE)
# Explicit Codex skill injection can be persisted as a structured <skill> fragment
# rather than a tool call. Implicit invocation is often only visible as a generic
# shell/file read of .../skills/<name>/SKILL.md. We detect both without storing
# prompt text, tool arguments, or file contents.
SKILL_XML_RE = re.compile(r"<skill\b[^>]*>[\s\S]*?<name>\s*([^<]+?)\s*</name>[\s\S]*?</skill>", re.IGNORECASE)
SKILL_PATH_RE = re.compile(r"(?:^|[\\/])skills[\\/](?:[^\\/\\r\\n\\\"\'<>]+[\\/])*?([^\\/\\r\\n\\\"\'<>]+)[\\/](?:SKILL|skill)\\.md\\b", re.IGNORECASE)
# Codex desktop can persist a concise activity summary such as
# "Read Codebase Memory skill" without exposing the underlying SKILL.md path.
# Only treat these phrases as invocations when the name resolves to a configured
# local skill; this avoids counting ordinary conversation text as skill usage.
SKILL_ACTION_RE = re.compile(
    r"\\b(?:read|load(?:ed)?|use(?:d|ing)?|invoke(?:d|ing)?|run|ran)\\s+"
    r"(?:the\\s+)?[`\'\\\"]?([A-Za-z0-9][A-Za-z0-9 _./:+\\-]{0,120}?)[`\'\\\"]?\\s+skill\\b",
    re.IGNORECASE,
)
MCP_CALL_RE = re.compile(r"(?:tools\\.)?mcp__([A-Za-z0-9_]+)__([A-Za-z0-9_]+)")


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    p = argparse.ArgumentParser(description="Extract Codex usage for a local calendar date range.")
    p.add_argument("--days", type=int, help="Local calendar days to rebuild, including today.")
    p.add_argument("--from-date", help="First local date to rebuild (YYYY-MM-DD).")
    p.add_argument("--to-date", help="Last local date to rebuild, inclusive (YYYY-MM-DD).")
    p.add_argument("--codex-home", type=Path, default=default_home, help=f"Codex data directory (default: {default_home}).")
    p.add_argument("--output-dir", type=Path, default=here / "data", help="Output directory (default: ./data).")
    p.add_argument("--scan-all", action="store_true", help="Scan every rollout rather than narrowing by mtime.")
    p.add_argument("--sqlite-only", action="store_true", help="Incrementally update only SQLite for the selected dates; skip CSV exports.")
    p.add_argument("--verbose", action="store_true", help="Print malformed/skipped details.")
    return p.parse_args()


def resolve_selected_dates(args: argparse.Namespace, local_tz) -> list[date]:
    today = datetime.now(local_tz).date()
    using_range = bool(args.from_date or args.to_date)
    if args.days is not None and using_range:
        raise ValueError("Use either --days or --from-date/--to-date, not both.")
    if using_range:
        if not args.from_date or not args.to_date:
            raise ValueError("--from-date and --to-date must be provided together.")
        try:
            start = date.fromisoformat(args.from_date)
            end = date.fromisoformat(args.to_date)
        except ValueError as exc:
            raise ValueError("Dates must use YYYY-MM-DD.") from exc
        if end < start:
            raise ValueError("--to-date must be on or after --from-date.")
    else:
        days = 7 if args.days is None else args.days
        if days < 1:
            raise ValueError("--days must be at least 1.")
        end = today
        start = today - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


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


def estimate_api_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> tuple[float, str, bool]:
    """Estimate today's Standard API token charge for one recorded model response."""
    original = model_key(model)
    priced_model = API_RATE_ALIASES.get(original, original)
    rates = API_RATES.get(priced_model)
    if not rates:
        return 0.0, "unpriced_model", False

    inp_rate, cached_rate, write_rate, out_rate = rates
    long_context = input_tokens > API_LONG_CONTEXT_THRESHOLD and priced_model in API_LONG_CONTEXT_MODELS
    if long_context:
        inp_rate *= 2.0
        cached_rate *= 2.0
        write_rate *= 2.0
        out_rate *= 1.5

    ordinary = max(0, input_tokens - cached_tokens - cache_write_tokens)
    cost = (
        ordinary * inp_rate
        + cached_tokens * cached_rate
        + cache_write_tokens * write_rate
        + output_tokens * out_rate
    ) / 1_000_000.0

    status = "priced_long_context" if long_context else "priced_standard"
    if original == "codex-auto-review":
        status += "_proxy_gpt-5.4"
    return cost, status, long_context


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


def load_configured_skills(codex_home: Path, verbose: bool = False) -> list[dict[str, str]]:
    """Inventory local Codex skills without persisting their instructions/content."""
    skills_root = codex_home / "skills"
    if not skills_root.exists():
        return []
    found: dict[str, dict[str, str]] = {}
    paths = sorted(set(skills_root.rglob("SKILL.md")) | set(skills_root.rglob("skill.md")))
    for path in paths:
        name = path.parent.name
        try:
            # Frontmatter name is authoritative where present. Read only enough
            # to identify it; no skill instructions are stored in analytics.
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
                line = raw.strip()
                if line.lower().startswith("name:"):
                    candidate = line.split(":", 1)[1].strip().strip("'\"")
                    if candidate:
                        name = candidate
                    break
        except OSError as exc:
            if verbose:
                print(f"Warning: could not inspect skill {path}: {exc}", file=sys.stderr)
        clean = clean_skill_name(name)
        if not clean:
            continue
        try:
            rel = path.relative_to(codex_home).as_posix()
        except ValueError:
            rel = path.name
        found[clean.lower()] = {"name": clean, "skill_file": rel}
    return sorted(found.values(), key=lambda row: row["name"].lower())


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


def iter_string_values(value: Any) -> Iterable[str]:
    """Yield string leaves from a rollout payload without retaining their content."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_string_values(child)


def clean_skill_name(value: Any) -> str:
    name = str(value or "").strip().strip("$`")
    return name[:200]


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
    seen_skill_events: set[tuple[str, str]] = set()

    def stage_skill(skill: Any, call_id: str, turn_id: str, timestamp: datetime | None, ctx: dict[str, str]) -> None:
        name = clean_skill_name(skill)
        if not name or not timestamp:
            return
        key = (str(turn_id or ""), name.lower())
        # Codex itself de-duplicates implicit skill invocation per turn; mirror that
        # behaviour so an injected fragment plus a subsequent SKILL.md read is one use.
        if key in seen_skill_events:
            return
        seen_skill_events.add(key)
        skill_events.append({"call_id": call_id or f"skill:{turn_id}:{name}", "skill": name, "turn_id": turn_id, "timestamp": timestamp, "ctx": dict(ctx)})


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
            ctx_for_item = turn_context.get(item_turn, current_ctx)

            # Explicitly injected skills can appear in the rollout as structured
            # <skill><name>...</name>...</skill> prompt fragments with no tool call.
            # Extract only the skill name; do not persist the surrounding prompt.
            for fragment in iter_string_values(payload):
                for match in SKILL_XML_RE.finditer(fragment):
                    stage_skill(match.group(1), f"skill-injected:{item_turn}:{match.group(1)}", item_turn, ts_utc, ctx_for_item)
            if rt.lower() in {"skill", "skill_invocation"}:
                stage_skill(payload.get("name") or payload.get("skill_name"), str(payload.get("id") or ""), item_turn, ts_utc, ctx_for_item)

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
                        stage_skill(skill, cid, item_turn, ts_utc, ctx)
                # Implicit Codex skill invocation may only be visible as a generic
                # shell/file read of a local .../skills/<name>/SKILL.md path.
                for m in SKILL_PATH_RE.finditer(text):
                    skill = clean_skill_name(m.group(1))
                    if skill and skill not in seen_skills:
                        seen_skills.add(skill)
                        stage_skill(skill, cid, item_turn, ts_utc, ctx)
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
    #
    # Newer Codex builds persist authoritative per-response token_usage_record
    # events. Older builds persist cumulative token_count snapshots instead.
    # For the latter, derive each request from the increase in total_token_usage;
    # this avoids double-counting repeated last_token_usage values on
    # rate-limit-only updates.
    records: list[dict[str, Any]] = []
    active_turn_id = ""
    current_ctx = {"model": "", "effort": "", "service_tier": ""}
    direct_usage_times = [
        parse_iso(i.get("timestamp"))
        for i in items
        if i.get("type") == "token_usage_record" and isinstance(i.get("payload"), dict)
    ]
    direct_usage_times = [dt for dt in direct_usage_times if dt is not None]
    first_direct_usage_utc = min(direct_usage_times) if direct_usage_times else None
    previous_cumulative: dict[str, int] | None = None
    legacy_usage_ordinal = 0

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
        elif typ == "event_msg" and str(payload.get("type") or "") == "token_count":
            info = payload.get("info") or {}
            total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total_usage, dict):
                continue

            current_cumulative = {
                "input_tokens": to_int(total_usage.get("input_tokens")),
                "cached_input_tokens": to_int(total_usage.get("cached_input_tokens")),
                "cache_write_input_tokens": to_int(total_usage.get("cache_write_input_tokens", total_usage.get("cache_creation_input_tokens", 0))),
                "output_tokens": to_int(total_usage.get("output_tokens")),
                "reasoning_output_tokens": to_int(total_usage.get("reasoning_output_tokens")),
                "total_tokens": to_int(total_usage.get("total_tokens")),
            }

            # Always advance the cumulative baseline, including events outside
            # the selected window. That makes the first in-range delta correct.
            if previous_cumulative is None:
                delta = dict(current_cumulative)
            else:
                delta = {
                    k: max(0, current_cumulative[k] - previous_cumulative.get(k, 0))
                    for k in current_cumulative
                }
            cumulative_advanced = any(current_cumulative[k] > (previous_cumulative or {}).get(k, 0) for k in current_cumulative)
            previous_cumulative = current_cumulative

            ts_utc = parse_iso(item.get("timestamp"))
            # Once a rollout starts emitting direct per-response telemetry,
            # prefer it and stop materialising cumulative token_count deltas.
            if not ts_utc or (first_direct_usage_utc is not None and ts_utc >= first_direct_usage_utc):
                continue
            if not cumulative_advanced or delta["total_tokens"] <= 0:
                continue

            ts_local = ts_utc.astimezone(local_tz)
            date_key = ts_local.date().isoformat()
            if date_key not in selected_dates:
                continue

            tid = str(payload.get("turn_id") or active_turn_id)
            ctx = turn_context.get(tid, current_ctx)
            thread_id = thread_id_default
            session_id = session_id_default or thread_id
            if not thread_id:
                continue

            inp = delta["input_tokens"]
            cached = delta["cached_input_tokens"]
            cache_write = delta["cache_write_input_tokens"]
            out = delta["output_tokens"]
            reasoning = delta["reasoning_output_tokens"]
            total = delta["total_tokens"] or inp + out
            fresh = max(0, inp - cached)
            credits, rate_status = estimate_credits(ctx.get("model", ""), fresh, cached, out, ctx.get("service_tier", ""))
            api_cost, api_status, api_long = estimate_api_cost(ctx.get("model", ""), inp, cached, cache_write, out)
            legacy_usage_ordinal += 1
            response_id = f"legacy-token-count:{legacy_usage_ordinal}:{iso_utc(ts_utc)}"

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
                "turn_id": tid, "root_turn_id": "", "response_id": response_id,
                "is_compaction": "false",
                "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": cache_write, "fresh_input_tokens": fresh,
                "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": total,
                "cache_hit_pct": round((cached / inp * 100.0) if inp else 0.0, 4),
                "estimated_credits": round(credits, 6), "credit_rate_status": rate_status,
                "api_equivalent_cost_usd": round(api_cost, 6), "api_cost_rate_status": api_status,
                "api_long_context": "true" if api_long else "false",
                "usage_source": "token_count_delta", "source_rollout": rel,
            })

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
            api_cost, api_status, api_long = estimate_api_cost(ctx.get("model", ""), inp, cached, cache_write, out)
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
                "estimated_credits": round(credits, 6), "credit_rate_status": rate_status,
                "api_equivalent_cost_usd": round(api_cost, 6), "api_cost_rate_status": api_status,
                "api_long_context": "true" if api_long else "false",
                "usage_source": "token_usage_record", "source_rollout": rel,
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
    cache_write = to_int(row.get("cache_write_input_tokens"))
    fresh = to_int(row.get("fresh_input_tokens")) or max(0, inp - cached)
    row["fresh_input_tokens"] = fresh
    credits, status = estimate_credits(str(row.get("model") or ""), fresh, cached, out, str(row.get("service_tier") or ""))
    row["estimated_credits"] = round(credits, 6)
    row["credit_rate_status"] = status
    api_cost, api_status, api_long = estimate_api_cost(str(row.get("model") or ""), inp, cached, cache_write, out)
    row["api_equivalent_cost_usd"] = round(api_cost, 6)
    row["api_cost_rate_status"] = api_status
    row["api_long_context"] = "true" if api_long else "false"
    if not str(row.get("usage_source") or ""):
        row["usage_source"] = "token_usage_record"
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
        g = grouped.setdefault(day, {"date":day,"responses":0,"sessions":set(),"threads":set(),"models":set(),"input_tokens":0,"cached_input_tokens":0,"fresh_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":0,"estimated_credits":0.0,"api_equivalent_cost_usd":0.0,"compaction_responses":0,"priced_tokens":0,"api_priced_tokens":0})
        g["responses"] += 1
        if r.get("session_id"): g["sessions"].add(r["session_id"])
        if r.get("thread_id"): g["threads"].add(r["thread_id"])
        if r.get("model"): g["models"].add(r["model"])
        for f in ("input_tokens","cached_input_tokens","fresh_input_tokens","output_tokens","reasoning_output_tokens","total_tokens"):
            g[f] += to_int(r.get(f))
        g["estimated_credits"] += to_float(r.get("estimated_credits"))
        g["api_equivalent_cost_usd"] += to_float(r.get("api_equivalent_cost_usd"))
        if str(r.get("credit_rate_status") or "").startswith("priced"): g["priced_tokens"] += to_int(r.get("total_tokens"))
        if str(r.get("api_cost_rate_status") or "").startswith("priced"): g["api_priced_tokens"] += to_int(r.get("total_tokens"))
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
            "api_equivalent_cost_usd":round(g["api_equivalent_cost_usd"],6),"api_cost_coverage_pct":round((g["api_priced_tokens"]/tot*100.0) if tot else 0.0,4),
            "compaction_responses":g["compaction_responses"],"failed_or_aborted_turns":failures[day],"turn_duration_ms":duration[day],"tool_calls":tool_counts[day],"skill_uses":skill_counts[day],"patch_lines_changed":code_lines[day],
        })
    return out


def write_sqlite_snapshot(
    path: Path,
    records: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    limits: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    configured_agents: list[dict[str, str]],
    configured_skills: list[dict[str, str]],
    metadata: dict[str, Any],
    daily_fields: list[str],
) -> None:
    """Write the complete analytics snapshot to SQLite, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    def sql_type(field: str, ints: set[str], floats: set[str]) -> str:
        if field in ints:
            return "INTEGER"
        if field in floats:
            return "REAL"
        return "TEXT"

    def create_and_insert(
        conn: sqlite3.Connection,
        table: str,
        fields: list[str],
        rows: list[dict[str, Any]],
        ints: set[str],
        floats: set[str],
    ) -> None:
        columns = ", ".join(f'"{field}" {sql_type(field, ints, floats)}' for field in fields)
        conn.execute(f'CREATE TABLE "{table}" ({columns})')
        if not rows:
            return
        placeholders = ",".join("?" for _ in fields)
        values = [[row.get(field, 0 if field in ints or field in floats else "") for field in fields] for row in rows]
        column_names = ",".join(f'"{field}"' for field in fields)
        conn.executemany(
            f'INSERT INTO "{table}" ({column_names}) VALUES ({placeholders})',
            values,
        )

    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        create_and_insert(conn, "responses", RECORD_FIELDS, records, RECORD_INT_FIELDS, RECORD_FLOAT_FIELDS)
        create_and_insert(conn, "turns", TURN_FIELDS, turns, TURN_INT_FIELDS, TURN_FLOAT_FIELDS)
        create_and_insert(conn, "activities", ACTIVITY_FIELDS, activities, ACTIVITY_INT_FIELDS, set())
        create_and_insert(conn, "rate_limits", LIMIT_FIELDS, limits, LIMIT_INT_FIELDS, LIMIT_FLOAT_FIELDS)
        create_and_insert(conn, "daily", daily_fields, daily, set(), {
            "cache_hit_pct", "estimated_credits", "credit_coverage_pct",
            "api_equivalent_cost_usd", "api_cost_coverage_pct",
        })
        create_and_insert(conn, "configured_agents", AGENT_FIELDS, configured_agents, set(), set())
        create_and_insert(conn, "configured_skills", SKILL_FIELDS, configured_skills, set(), set())
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            [(str(k), json.dumps(v, ensure_ascii=False, separators=(",", ":"))) for k, v in metadata.items()],
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
            conn.execute(f'CREATE INDEX "{name}" ON "{table}" ({cols})')
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp, path)


def sqlite_insert_rows(
    conn: sqlite3.Connection,
    table: str,
    fields: list[str],
    rows: list[dict[str, Any]],
    ints: set[str],
    floats: set[str],
) -> None:
    if not rows:
        return
    placeholders = ",".join("?" for _ in fields)
    column_names = ",".join(f'"{field}"' for field in fields)
    values = [
        [row.get(field, 0 if field in ints or field in floats else "") for field in fields]
        for row in rows
    ]
    conn.executemany(
        f'INSERT INTO "{table}" ({column_names}) VALUES ({placeholders})',
        values,
    )


def update_sqlite_range(
    path: Path,
    selected: set[str],
    records: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    limits: list[dict[str, Any]],
    agents: list[dict[str, str]],
    skills: list[dict[str, str]],
    metadata_updates: dict[str, Any],
    daily_fields: list[str],
) -> dict[str, Any]:
    """Replace only selected date partitions in an existing SQLite database."""
    if not path.exists():
        raise FileNotFoundError(path)

    daily = daily_summary(records, turns, activities)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        date_values = sorted(selected)
        placeholders = ",".join("?" for _ in date_values)
        for table in ("responses", "turns", "activities", "rate_limits", "daily"):
            conn.execute(f'DELETE FROM "{table}" WHERE date IN ({placeholders})', date_values)

        sqlite_insert_rows(conn, "responses", RECORD_FIELDS, records, RECORD_INT_FIELDS, RECORD_FLOAT_FIELDS)
        sqlite_insert_rows(conn, "turns", TURN_FIELDS, turns, TURN_INT_FIELDS, TURN_FLOAT_FIELDS)
        sqlite_insert_rows(conn, "activities", ACTIVITY_FIELDS, activities, ACTIVITY_INT_FIELDS, set())
        sqlite_insert_rows(conn, "rate_limits", LIMIT_FIELDS, limits, LIMIT_INT_FIELDS, LIMIT_FLOAT_FIELDS)
        sqlite_insert_rows(conn, "daily", daily_fields, daily, set(), {
            "cache_hit_pct", "estimated_credits", "credit_coverage_pct",
            "api_equivalent_cost_usd", "api_cost_coverage_pct",
        })

        conn.execute("DELETE FROM configured_agents")
        sqlite_insert_rows(conn, "configured_agents", AGENT_FIELDS, agents, set(), set())
        conn.execute("DELETE FROM configured_skills")
        sqlite_insert_rows(conn, "configured_skills", SKILL_FIELDS, skills, set(), set())

        counts = conn.execute(
            """SELECT COUNT(*) responses,
                      COUNT(DISTINCT thread_id) threads,
                      MIN(date) first_date,
                      MAX(date) last_date,
                      COALESCE(SUM(total_tokens),0) total_tokens,
                      COALESCE(SUM(CASE WHEN credit_rate_status LIKE 'priced%' THEN total_tokens ELSE 0 END),0) priced_tokens
                 FROM responses"""
        ).fetchone()
        turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        activity_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

        metadata_updates.update({
            "dataset_responses": int(counts["responses"] or 0),
            "dataset_turns": int(turn_count or 0),
            "dataset_activities": int(activity_count or 0),
            "dataset_first_date": counts["first_date"],
            "dataset_last_date": counts["last_date"],
            "configured_agents": len(agents),
            "configured_skills": len(skills),
            "credit_coverage_pct": round(
                (float(counts["priced_tokens"] or 0) / float(counts["total_tokens"] or 1) * 100.0)
                if counts["total_tokens"] else 0.0,
                4,
            ),
        })
        for key, value in metadata_updates.items():
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
            )
        conn.commit()
        return metadata_updates
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    args=parse_args()
    local_tz=datetime.now().astimezone().tzinfo
    try:
        dates=resolve_selected_dates(args, local_tz)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    codex_home=args.codex_home.expanduser().resolve(); output_dir=args.output_dir.expanduser().resolve()
    if not codex_home.exists():
        print(f"Codex home not found: {codex_home}",file=sys.stderr); return 1

    records_path=output_dir/"codex_usage_records.csv"; turns_path=output_dir/"codex_turns.csv"; activity_path=output_dir/"codex_activity.csv"; limits_path=output_dir/"codex_rate_limits.csv"
    daily_path=output_dir/"codex_usage_daily.csv"; database_path=output_dir/"codex_usage.sqlite"; metadata_path=output_dir/"codex_usage_metadata.json"; agents_path=output_dir/"codex_agents.csv"; skills_path=output_dir/"codex_skills.csv"

    selected={d.isoformat() for d in dates}; earliest=min(dates); earliest_local=datetime.combine(earliest,datetime.min.time(),tzinfo=local_tz)
    print(f"Preparing {len(selected)} day refresh…", flush=True)
    names=load_thread_names(codex_home,args.verbose); agents=load_configured_agents(codex_home,args.verbose); skills=load_configured_skills(codex_home,args.verbose); rollouts=discover_rollouts(codex_home,earliest_local,args.scan_all)
    print(f"Scanning {len(rollouts):,} rollout file(s)…", flush=True)

    rebuilt_records=[]; rebuilt_turns=[]; rebuilt_activity=[]; rebuilt_limits=[]; files_with_usage=0
    total_rollouts=len(rollouts)
    progress_every=max(1,total_rollouts//10) if total_rollouts else 1
    for idx,path in enumerate(rollouts,1):
        r,t,a,l=parse_rollout(path,selected,local_tz,names,codex_home,args.verbose)
        if r or t or a: files_with_usage+=1
        rebuilt_records.extend(r); rebuilt_turns.extend(t); rebuilt_activity.extend(a); rebuilt_limits.extend(l)
        if idx==total_rollouts or idx%progress_every==0:
            print(f"Parsed {idx:,}/{total_rollouts:,} rollout file(s)…", flush=True)

    rebuilt_records=dedupe_records(rebuilt_records)
    rebuilt_turns=dedupe_turns(rebuilt_turns); rebuilt_turns=enrich_turns(rebuilt_turns,rebuilt_records)
    rebuilt_activity=dedupe_activity(rebuilt_activity)
    rebuilt_limits=dedupe_limits(rebuilt_limits)

    daily_fields=["date","responses","turns","sessions","threads","models","input_tokens","cached_input_tokens","fresh_input_tokens","output_tokens","reasoning_output_tokens","total_tokens","cache_hit_pct","estimated_credits","credit_coverage_pct","api_equivalent_cost_usd","api_cost_coverage_pct","compaction_responses","failed_or_aborted_turns","turn_duration_ms","tool_calls","skill_uses","patch_lines_changed"]

    now=datetime.now(local_tz)
    common_metadata={
        "generated_at":now.isoformat(),"local_timezone":str(local_tz),"codex_home":str(codex_home),
        "processed_dates":sorted(selected),"days_requested":len(dates),
        "requested_from":min(selected),"requested_to":max(selected),
        "rollout_files_scanned":len(rollouts),"rollout_files_with_selected_activity":files_with_usage,
        "rebuilt_responses":len(rebuilt_records),
        "credit_rate_as_of":CREDIT_RATE_AS_OF,"credit_rate_source":CREDIT_RATE_SOURCE,
        "credit_note":"Estimated token-based Codex credits using the current Business rate card. Cache writes are not charged. Fast/priority multipliers are applied only where publicly documented; unpriced models/tier combinations are excluded rather than guessed.",
        "api_price_as_of":API_PRICE_AS_OF,"api_price_source":API_PRICE_SOURCE,
        "api_cost_note":"Counterfactual Standard OpenAI API token cost at today's rates. Includes cached-input and cache-write pricing plus documented >272K long-context multipliers. Excludes separate tool-call, web-search, container, storage, regional-processing, and other non-token API charges.",
        "accounting_note":"Newer usage comes from per-response token_usage_record rows. Older rollouts are recovered from positive deltas in token_count.info.total_token_usage, which avoids repeated last_token_usage snapshots. Rows are deduplicated by (thread_id,response_id). Reasoning output is a subset of output and is not added again. Tool/activity datasets store names/counts only, not prompt/tool content.",
    }

    if args.sqlite_only and database_path.exists():
        print("Updating selected SQLite date partitions…", flush=True)
        metadata=update_sqlite_range(
            database_path,selected,rebuilt_records,rebuilt_turns,rebuilt_activity,rebuilt_limits,
            agents,skills,common_metadata,daily_fields,
        )
        print(f"Refresh complete: {len(rebuilt_records):,} responses, {len(rebuilt_turns):,} turns, {len(rebuilt_activity):,} activities.", flush=True)
        return 0

    if args.sqlite_only and not database_path.exists():
        print("SQLite database does not exist yet; falling back to a full initial build.", flush=True)

    print("Loading retained export history…", flush=True)
    existing_records=load_csv(records_path,RECORD_FIELDS,RECORD_INT_FIELDS,RECORD_FLOAT_FIELDS)
    existing_turns=load_csv(turns_path,TURN_FIELDS,TURN_INT_FIELDS,TURN_FLOAT_FIELDS)
    existing_activity=load_csv(activity_path,ACTIVITY_FIELDS,ACTIVITY_INT_FIELDS,set())
    existing_limits=load_csv(limits_path,LIMIT_FIELDS,LIMIT_INT_FIELDS,LIMIT_FLOAT_FIELDS)
    retained_records=[r for r in existing_records if str(r.get("date") or "") not in selected]
    retained_turns=[r for r in existing_turns if str(r.get("date") or "") not in selected]
    retained_activity=[r for r in existing_activity if str(r.get("date") or "") not in selected]
    retained_limits=[r for r in existing_limits if str(r.get("date") or "") not in selected]

    records=dedupe_records(retained_records+rebuilt_records)
    turns=dedupe_turns(retained_turns+rebuilt_turns); turns=enrich_turns(turns,records)
    activities=dedupe_activity(retained_activity+rebuilt_activity)
    limits=dedupe_limits(retained_limits+rebuilt_limits)

    write_csv_atomic(records_path,records,RECORD_FIELDS); write_csv_atomic(turns_path,turns,TURN_FIELDS); write_csv_atomic(activity_path,activities,ACTIVITY_FIELDS); write_csv_atomic(limits_path,limits,LIMIT_FIELDS); write_csv_atomic(agents_path,agents,AGENT_FIELDS); write_csv_atomic(skills_path,skills,SKILL_FIELDS)
    daily=daily_summary(records,turns,activities)
    write_csv_atomic(daily_path,daily,daily_fields)

    priced_tokens=sum(to_int(r.get("total_tokens")) for r in records if str(r.get("credit_rate_status") or "").startswith("priced")); all_tokens=sum(to_int(r.get("total_tokens")) for r in records)
    metadata={
        "generated_at":now.isoformat(),"local_timezone":str(local_tz),"codex_home":str(codex_home),"processed_dates":sorted(selected),"days_requested":len(dates),
        "requested_from":min(selected),"requested_to":max(selected),
        "rollout_files_scanned":len(rollouts),"rollout_files_with_selected_activity":files_with_usage,"rebuilt_responses":len(dedupe_records(rebuilt_records)),"dataset_responses":len(records),"dataset_turns":len(turns),"dataset_activities":len(activities),
        "dataset_first_date":min((r["date"] for r in records),default=None),"dataset_last_date":max((r["date"] for r in records),default=None),"configured_agents":len(agents),"configured_skills":len(skills),
        "credit_rate_as_of":CREDIT_RATE_AS_OF,"credit_rate_source":CREDIT_RATE_SOURCE,"credit_coverage_pct":round((priced_tokens/all_tokens*100.0) if all_tokens else 0.0,4),
        "credit_note":"Estimated token-based Codex credits using the current Business rate card. Cache writes are not charged. Fast/priority multipliers are applied only where publicly documented; unpriced models/tier combinations are excluded rather than guessed.",
        "api_price_as_of":API_PRICE_AS_OF,"api_price_source":API_PRICE_SOURCE,
        "api_cost_note":"Counterfactual Standard OpenAI API token cost at today's rates. Includes cached-input and cache-write pricing plus documented >272K long-context multipliers. Excludes separate tool-call, web-search, container, storage, regional-processing, and other non-token API charges.",
        "accounting_note":"Newer usage comes from per-response token_usage_record rows. Older rollouts are recovered from positive deltas in token_count.info.total_token_usage, which avoids repeated last_token_usage snapshots. Rows are deduplicated by (thread_id,response_id). Reasoning output is a subset of output and is not added again. Tool/activity datasets store names/counts only, not prompt/tool content.",
        "usage_source_counts":dict(sorted({src:sum(1 for r in records if str(r.get("usage_source") or "")==src) for src in {str(r.get("usage_source") or "") for r in records if r.get("usage_source")}}.items())),
    }
    write_sqlite_snapshot(database_path,records,turns,activities,limits,daily,agents,skills,metadata,daily_fields)
    stale_browser_bundle=output_dir/"codex_usage_data.js"
    try:
        stale_browser_bundle.unlink()
    except FileNotFoundError:
        pass
    with metadata_path.open("w",encoding="utf-8") as f: json.dump(metadata,f,indent=2); f.write("\n")

    ds=sorted(selected)
    print(f"Requested window: {len(ds)} day(s), {ds[0]} through {ds[-1]}")
    observed_dates=sorted({str(r.get("date") or "") for r in records if r.get("date")})
    if observed_dates:
        print(f"Observed token data: {observed_dates[0]} through {observed_dates[-1]} ({len(observed_dates)} day(s) with usage)")
    else:
        print("Observed token data: none")
    source_counts=metadata.get("usage_source_counts") or {}
    if source_counts:
        print("Usage sources: " + ", ".join(f"{k}={v:,}" for k,v in source_counts.items()))
    print(f"Scanned {len(rollouts)} rollout file(s); {files_with_usage} contained selected activity")
    print(f"Rebuilt {len(dedupe_records(rebuilt_records)):,} response record(s), {len(dedupe_turns(rebuilt_turns)):,} turn(s), {len(dedupe_activity(rebuilt_activity)):,} activity record(s)")
    print(f"Dataset: {len(records):,} responses | {len(turns):,} turns | {len(activities):,} activities")
    print(f"Credit estimate coverage: {metadata['credit_coverage_pct']:.1f}% of token volume")
    try:
        database_mb = database_path.stat().st_size / (1024 * 1024)
        print(f"SQLite database: {database_mb:.1f} MB")
    except OSError:
        pass
    print(f"SQLite: {database_path}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
