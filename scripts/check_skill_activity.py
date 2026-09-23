#!/usr/bin/env python3
"""Regression tests for Codex implicit skill invocation detection."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect_codex_usage import parse_rollout


def item(ts: str, typ: str, payload: dict) -> dict:
    return {"timestamp": ts, "type": typ, "payload": payload}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        codex_home = Path(td)
        rollout = codex_home / "sessions" / "2026" / "09" / "23" / "rollout-skill-access.jsonl"
        rollout.parent.mkdir(parents=True)

        rows = [
            item("2026-09-23T00:00:00Z", "session_meta", {"id": "thread-1", "cwd": "C:\\repo"}),
            item("2026-09-23T00:00:00.100Z", "turn_context", {
                "turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "medium"
            }),
            # Prose alone is not evidence of an invocation.
            item("2026-09-23T00:00:00.200Z", "response_item", {
                "type": "message", "role": "assistant", "turn_id": "turn-1",
                "content": [{"type": "output_text", "text": "Read Codebase Memory skill"}]
            }),
            # A wildcard/search mentioning the skill and SKILL.md is also not an
            # invocation; Codex's own detection requires a concrete skill access.
            item("2026-09-23T00:00:00.300Z", "response_item", {
                "type": "custom_tool_call", "name": "exec", "call_id": "search-only",
                "turn_id": "turn-1",
                "input": json.dumps({
                    "command": "rg -n 'Codebase Memory' $env:USERPROFILE/.codex/skills/**/SKILL.md"
                }),
            }),
            # Concrete PowerShell read of the configured skill document.
            item("2026-09-23T00:00:01Z", "response_item", {
                "type": "custom_tool_call", "name": "exec", "call_id": "read-skill",
                "turn_id": "turn-1",
                "input": json.dumps({
                    "command": "Get-Content $env:USERPROFILE/.codex/skills/codebase-memory/SKILL.md"
                }),
            }),
            # Script execution under the skill also counts as implicit use.
            item("2026-09-23T00:00:02Z", "response_item", {
                "type": "custom_tool_call", "name": "exec", "call_id": "run-skill-script",
                "turn_id": "turn-2",
                "input": json.dumps({
                    "command": "python $env:USERPROFILE/.codex/skills/codebase-memory/scripts/check.py"
                }),
            }),
            # Newer Codex builds can expose a first-class skills.read tool.
            item("2026-09-23T00:00:03Z", "response_item", {
                "type": "function_call", "name": "skills.read", "call_id": "skills-read",
                "turn_id": "turn-3",
                "arguments": json.dumps({"package": "codebase-memory"}),
            }),
        ]
        rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        configured = [{
            "name": "codebase-memory",
            "skill_file": "skills/codebase-memory/SKILL.md",
        }]
        _records, _turns, activities, _limits = parse_rollout(
            rollout,
            {"2026-09-23"},
            timezone.utc,
            {},
            codex_home,
            True,
            configured,
        )

        skills = [row for row in activities if row.get("activity_type") == "skill"]
        assert len(skills) == 3, skills
        assert {row["skill_name"] for row in skills} == {"codebase-memory"}, skills
        assert {row["call_id"] for row in skills} == {
            "read-skill", "run-skill-script", "skills-read"
        }, skills

    print("Codex implicit skill invocation regression test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
