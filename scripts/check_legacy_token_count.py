#!/usr/bin/env python3
"""Regression test for pre-token_usage_record Codex token_count telemetry."""
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


def row(ts: str, typ: str, payload: dict) -> dict:
    return {"timestamp": ts, "type": typ, "payload": payload}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        codex_home = Path(td)
        rollout = codex_home / "sessions" / "2026" / "06" / "30" / "rollout-test.jsonl"
        rollout.parent.mkdir(parents=True)
        items = [
            row("2026-06-30T00:00:00Z", "session_meta", {"id": "thread-1", "cwd": "C:\\repo"}),
            row("2026-06-30T00:00:00.100Z", "turn_context", {"turn_id": "turn-1", "model": "gpt-5.6-sol", "effort": "medium"}),
            row("2026-06-30T00:00:01Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 10, "reasoning_output_tokens": 2,
                        "total_tokens": 110
                    },
                    "last_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 10, "reasoning_output_tokens": 2,
                        "total_tokens": 110
                    }
                }
            }),
            # Rate-limit-only rebroadcast: total usage is unchanged and must not count again.
            row("2026-06-30T00:00:02Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 10, "reasoning_output_tokens": 2,
                        "total_tokens": 110
                    },
                    "last_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 10, "reasoning_output_tokens": 2,
                        "total_tokens": 110
                    }
                },
                "rate_limits": {"primary": {"used_percent": 10}}
            }),
            row("2026-06-30T00:00:03Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 180, "cached_input_tokens": 20,
                        "output_tokens": 25, "reasoning_output_tokens": 5,
                        "total_tokens": 205
                    },
                    "last_token_usage": {
                        "input_tokens": 80, "cached_input_tokens": 20,
                        "output_tokens": 15, "reasoning_output_tokens": 3,
                        "total_tokens": 95
                    }
                }
            }),
            # New telemetry begins here. Earlier token_count deltas should be retained.
            row("2026-06-30T00:00:05Z", "token_usage_record", {
                "thread_id": "thread-1", "turn_id": "turn-1", "response_id": "response-1",
                "usage": {
                    "input_tokens": 50, "cached_input_tokens": 40,
                    "output_tokens": 5, "reasoning_output_tokens": 1,
                    "total_tokens": 55
                }
            }),
            # Once direct telemetry exists, later token_count snapshots are ignored.
            row("2026-06-30T00:00:06Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 230, "cached_input_tokens": 60,
                        "output_tokens": 30, "reasoning_output_tokens": 6,
                        "total_tokens": 260
                    }
                }
            }),
        ]
        rollout.write_text("\n".join(json.dumps(x) for x in items) + "\n", encoding="utf-8")
        records, _turns, _activities, _limits = parse_rollout(
            rollout, {"2026-06-30"}, timezone.utc, {}, codex_home, True
        )

        legacy = [r for r in records if r.get("usage_source") == "token_count_delta"]
        direct = [r for r in records if r.get("usage_source") == "token_usage_record"]
        assert len(legacy) == 2, legacy
        assert len(direct) == 1, direct
        assert sum(int(r["total_tokens"]) for r in legacy) == 205, legacy
        assert int(legacy[1]["input_tokens"]) == 80, legacy[1]
        assert int(legacy[1]["cached_input_tokens"]) == 20, legacy[1]
        assert int(legacy[1]["output_tokens"]) == 15, legacy[1]
        assert int(direct[0]["total_tokens"]) == 55, direct[0]

    print("Legacy token_count regression test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
