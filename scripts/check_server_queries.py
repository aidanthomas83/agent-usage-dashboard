#!/usr/bin/env python3
"""Integration checks for tab-specific SQLite dashboard queries."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collect_codex_usage as collector
import server


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        db = data_dir / "codex_usage.sqlite"
        record = {
            "date": "2026-09-23",
            "timestamp_utc": "2026-09-23T00:00:01Z",
            "timestamp_local": "2026-09-23T10:00:01+10:00",
            "session_id": "session-1",
            "session_name": "Integration test",
            "thread_id": "thread-1",
            "thread_name": "Integration test",
            "thread_start_local": "2026-09-23T10:00:00+10:00",
            "thread_latest_local": "2026-09-23T10:05:00+10:00",
            "agent_type": "Subagent",
            "agent_role": "executor",
            "agent_label": "executor",
            "project": "test-project",
            "model": "gpt-6-sol",
            "reasoning_effort": "medium",
            "turn_id": "turn-1",
            "response_id": "response-1",
            "is_compaction": "false",
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "cache_write_input_tokens": 0,
            "fresh_input_tokens": 200,
            "output_tokens": 100,
            "reasoning_output_tokens": 20,
            "total_tokens": 1100,
            "estimated_credits": 0.0,
            "credit_rate_status": "priced_standard",
        }
        turn = {
            "date": "2026-09-23",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "agent_type": "Subagent",
            "agent_role": "executor",
            "agent_label": "executor",
            "project": "test-project",
            "turn_id": "turn-1",
            "model": "gpt-6-sol",
            "reasoning_effort": "medium",
            "status": "completed",
            "started_utc": "2026-09-23T00:00:00Z",
            "completed_utc": "2026-09-23T00:00:05Z",
            "duration_ms": 5000,
            "time_to_first_token_ms": 400,
            "context_utilization_pct": 20.0,
        }
        skill = {
            "date": "2026-09-23",
            "timestamp_utc": "2026-09-23T00:00:00Z",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "project": "test-project",
            "agent_type": "Subagent",
            "agent_role": "executor",
            "agent_label": "executor",
            "model": "gpt-6-sol",
            "reasoning_effort": "medium",
            "activity_type": "skill",
            "skill_name": "coding-standards",
            "call_id": "skill-1",
        }
        mcp = {
            "date": "2026-09-23",
            "timestamp_utc": "2026-09-23T00:00:01Z",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "project": "test-project",
            "agent_type": "Subagent",
            "agent_role": "executor",
            "agent_label": "executor",
            "model": "gpt-6-sol",
            "reasoning_effort": "medium",
            "activity_type": "plugin",
            "tool_name": "search_graph",
            "tool_category": "Codebase Memory",
            "plugin_name": "Codebase Memory",
            "call_id": "mcp-1",
            "status": "completed",
            "duration_ms": 1250,
        }
        limit = {
            "date": "2026-09-23",
            "timestamp_utc": "2026-09-23T00:00:02Z",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "model": "gpt-6-sol",
            "primary_used_pct": 8.0,
        }
        metadata = {"generated_at": "2026-09-23T10:05:00+10:00"}
        collector.write_sqlite_snapshot(
            db,
            [record],
            [turn],
            [skill, mcp],
            [limit],
            [{"date": "2026-09-23"}],
            [{"name": "executor"}],
            [{"name": "coding-standards", "skill_file": "skills/coding-standards/SKILL.md"},
             {"name": "unused-skill", "skill_file": "skills/unused-skill/SKILL.md"}],
            metadata,
            ["date"],
        )

        # Simulate the database created by the previous application version:
        # historical telemetry exists, but configured_skills/new indexes do not.
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE configured_skills")
            before = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

        skills_root = data_dir / "codex-home" / "skills"
        (skills_root / "coding-standards").mkdir(parents=True)
        (skills_root / "coding-standards" / "SKILL.md").write_text("---\nname: coding-standards\n---\n", encoding="utf-8")
        (skills_root / "unused-skill").mkdir(parents=True)
        (skills_root / "unused-skill" / "SKILL.md").write_text("---\nname: unused-skill\n---\n", encoding="utf-8")
        server.migrate_existing_database(data_dir, data_dir / "codex-home")

        with sqlite3.connect(db) as conn:
            after = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
            configured_count = conn.execute("SELECT COUNT(*) FROM configured_skills").fetchone()[0]
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert before == after == 1
        assert configured_count == 2
        assert "idx_responses_dashboard" in indexes
        assert "idx_responses_date_session" in indexes

        filters = {
            "from": "2026-09-23", "to": "2026-09-23",
            "model": "", "agent": "", "effort": "", "project": "",
        }
        token = server.token_payload(data_dir, filters)
        subscription = server.subscription_payload(data_dir, filters)
        insights = server.insights_payload(data_dir, filters)
        activity = server.activity_payload(data_dir, filters)
        sessions = server.sessions_payload(data_dir, filters)

        assert token["ready"] and token["summary"]["responses"] == 1, token
        assert subscription["ready"] and subscription["summary"]["api_cost"] > 0, subscription
        assert subscription["runtime_summary"]["agent_compute_ms"] == 5000, subscription["runtime_summary"]
        assert subscription["runtime_agents"][0]["name"] == "executor", subscription["runtime_agents"]
        assert subscription["runtime_agents"][0]["api_cost"] > 0, subscription["runtime_agents"]
        assert insights["skill_summary"]["skill_invocations"] == 1, insights
        assert activity["skill_invocations"] == 1 and activity["distinct_skills"] == 1, activity
        assert activity["mcp_integrations"][0]["name"] == "Codebase Memory", activity["mcp_integrations"]
        assert activity["mcp_integrations"][0]["calls"] == 1, activity["mcp_integrations"]
        assert activity["mcp_tools"][0]["tool"] == "search_graph", activity["mcp_tools"]
        runtime = activity["runtime_summary"]
        assert runtime["agent_compute_ms"] == 5000, runtime
        assert runtime["active_wall_ms"] == 5000, runtime
        assert runtime["duration_coverage_pct"] == 100.0, runtime
        assert runtime["interval_coverage_pct"] == 100.0, runtime
        assert runtime["cost_per_agent_hour"] > 0, runtime
        assert server.merged_interval_ms([
            {"started_utc": "2026-09-23T00:00:00Z", "completed_utc": "2026-09-23T00:00:10Z"},
            {"started_utc": "2026-09-23T00:00:05Z", "completed_utc": "2026-09-23T00:00:15Z"},
        ]) == 15000

        # A parent lifecycle copied into a subagent rollout is one turn. A
        # reused generic turn id at a different timestamp is still distinct.
        copied = collector.dedupe_turns([
            {
                "thread_id": "main-thread", "turn_id": "turn-a", "agent_type": "Main",
                "agent_role": "", "status": "completed",
                "started_utc": "2026-08-07T00:00:00Z",
                "completed_utc": "2026-08-07T00:10:00Z", "duration_ms": 600000,
            },
            {
                "thread_id": "child-thread", "turn_id": "turn-a", "agent_type": "Subagent",
                "agent_role": "executor", "status": "completed",
                "started_utc": "2026-08-07T00:00:00Z",
                "completed_utc": "2026-08-07T00:10:00Z", "duration_ms": 600000,
            },
            {
                "thread_id": "child-2", "turn_id": "rollout-2", "agent_type": "Subagent",
                "agent_role": "executor", "status": "completed",
                "started_utc": "2026-08-07T01:00:00Z",
                "completed_utc": "2026-08-07T01:01:00Z", "duration_ms": 60000,
            },
            {
                "thread_id": "child-3", "turn_id": "rollout-2", "agent_type": "Subagent",
                "agent_role": "reviewer", "status": "completed",
                "started_utc": "2026-08-07T02:00:00Z",
                "completed_utc": "2026-08-07T02:01:00Z", "duration_ms": 60000,
            },
        ])
        assert len(copied) == 3, copied
        canonical = [row for row in copied if row["turn_id"] == "turn-a"][0]
        assert canonical["agent_type"] == "Main", canonical

        statuses = {row["name"]: row["status"] for row in activity["configured_skills"]}
        assert statuses["coding-standards"] == "used_selected", statuses
        assert statuses["unused-skill"] == "never_seen", statuses
        assert len(sessions["sessions"]) == 1 and sessions["sessions"][0]["api_cost"] > 0, sessions

        # Incremental dashboard refresh must replace only selected dates and
        # leave historical partitions untouched.
        old_record = dict(record)
        old_record.update({
            "date": "2026-09-22",
            "timestamp_utc": "2026-09-22T00:00:01Z",
            "timestamp_local": "2026-09-22T10:00:01+10:00",
            "response_id": "response-old",
            "total_tokens": 500,
            "input_tokens": 450,
            "fresh_input_tokens": 100,
            "cached_input_tokens": 350,
            "output_tokens": 50,
        })
        with sqlite3.connect(db) as conn:
            fields = collector.RECORD_FIELDS
            columns = ",".join(f'"{f}"' for f in fields)
            placeholders = ",".join("?" for _ in fields)
            conn.execute(
                f'INSERT INTO responses ({columns}) VALUES ({placeholders})',
                [old_record.get(f, 0 if f in collector.RECORD_INT_FIELDS or f in collector.RECORD_FLOAT_FIELDS else "") for f in fields],
            )
            conn.commit()

        replacement_record = dict(record)
        replacement_record.update({
            "response_id": "response-replaced",
            "total_tokens": 2200,
            "input_tokens": 2000,
            "fresh_input_tokens": 300,
            "cached_input_tokens": 1700,
            "output_tokens": 200,
        })
        collector.update_sqlite_range(
            db,
            {"2026-09-23"},
            [replacement_record],
            [turn],
            [skill],
            [limit],
            [{"name": "executor"}],
            [{"name": "coding-standards", "skill_file": "skills/coding-standards/SKILL.md"}],
            {"generated_at": "2026-09-23T11:00:00+10:00"},
            ["date"],
        )
        with sqlite3.connect(db) as conn:
            sep22 = conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM responses WHERE date='2026-09-22'").fetchone()
            sep23 = conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM responses WHERE date='2026-09-23'").fetchone()
        assert sep22 == (1, 500), sep22
        assert sep23 == (1, 2200), sep23

    print("Server tab query and incremental refresh integration test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
