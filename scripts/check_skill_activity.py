#!/usr/bin/env python3
"""Regression test for Codex desktop skill activity summaries."""
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
        rollout = codex_home / "sessions" / "2026" / "09" / "23" / "rollout-skill-summary.jsonl"
        rollout.parent.mkdir(parents=True)

        rows = [
            item("2026-09-23T00:00:00Z", "session_meta", {"id": "thread-1", "cwd": "C:\\repo"}),
            item("2026-09-23T00:00:00.100Z", "turn_context", {
                "turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "medium"
            }),
            # User prose must not be interpreted as observed skill use.
            item("2026-09-23T00:00:00.200Z", "response_item", {
                "type": "message", "role": "user", "turn_id": "turn-user",
                "content": [{"type": "input_text", "text": "Read Codebase Memory skill"}]
            }),
            # This mirrors the concise Codex desktop activity shown in the UI.
            item("2026-09-23T00:00:01Z", "response_item", {
                "type": "message", "role": "assistant", "turn_id": "turn-1",
                "content": [{"type": "output_text", "text": "Read Codebase Memory skill"}]
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
        assert len(skills) == 1, skills
        assert skills[0]["skill_name"] == "codebase-memory", skills[0]
        assert skills[0]["turn_id"] == "turn-1", skills[0]

    print("Codex skill activity summary regression test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
