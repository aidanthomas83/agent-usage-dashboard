#!/usr/bin/env python3
"""Regression tests for pre-token_usage_record Codex token_count telemetry."""
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


def usage(inp: int, cached: int, out: int, reasoning: int, total: int) -> dict:
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        codex_home = Path(td)
        rollout = codex_home / "sessions" / "2026" / "06" / "30" / "rollout-test.jsonl"
        rollout.parent.mkdir(parents=True)

        items = [
            row("2026-06-30T00:00:00Z", "session_meta", {"id": "thread-1", "cwd": "C:\\repo"}),
            row("2026-06-30T00:00:00.100Z", "turn_context", {
                "turn_id": "turn-1", "model": "gpt-5.6-sol", "effort": "medium"
            }),

            # Forked/resumed rollout: the cumulative total already contains
            # 1M inherited tokens, while last_token_usage is only this response.
            row("2026-06-30T00:00:01Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(900000, 800000, 100000, 20000, 1000000),
                    "last_token_usage": usage(100, 0, 10, 2, 110),
                },
            }),

            # Rate-limit-only rebroadcast: cumulative usage is unchanged and
            # must not count the same last_token_usage again.
            row("2026-06-30T00:00:02Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(900000, 800000, 100000, 20000, 1000000),
                    "last_token_usage": usage(100, 0, 10, 2, 110),
                },
                "rate_limits": {"primary": {"used_percent": 10}},
            }),

            row("2026-06-30T00:00:03Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(900080, 800020, 100015, 20003, 1000095),
                    "last_token_usage": usage(80, 20, 15, 3, 95),
                },
            }),

            # Cumulative counters reset. The request at the reset must still
            # be counted from last_token_usage rather than being lost because
            # every cumulative field moved backwards.
            row("2026-06-30T00:00:04Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(70, 10, 10, 2, 80),
                    "last_token_usage": usage(70, 10, 10, 2, 80),
                },
            }),

            # New direct telemetry begins here. Earlier token_count usage stays;
            # later token_count snapshots are ignored.
            row("2026-06-30T00:00:05Z", "token_usage_record", {
                "thread_id": "thread-1", "turn_id": "turn-1", "response_id": "response-1",
                "usage": usage(50, 40, 5, 1, 55),
            }),
            row("2026-06-30T00:00:06Z", "event_msg", {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage(120, 50, 15, 3, 135),
                    "last_token_usage": usage(50, 40, 5, 1, 55),
                },
            }),
        ]
        rollout.write_text("\n".join(json.dumps(x) for x in items) + "\n", encoding="utf-8")

        records, _turns, _activities, _limits = parse_rollout(
            rollout, {"2026-06-30"}, timezone.utc, {}, codex_home, True
        )

        legacy = [r for r in records if r.get("usage_source") == "token_count_last_usage"]
        direct = [r for r in records if r.get("usage_source") == "token_usage_record"]

        assert len(legacy) == 3, legacy
        assert len(direct) == 1, direct

        # 110 + 95 + 80. The inherited one-million-token cumulative snapshot
        # must not be charged as a response.
        assert sum(int(r["total_tokens"]) for r in legacy) == 285, legacy
        assert int(legacy[0]["total_tokens"]) == 110, legacy[0]
        assert int(legacy[1]["input_tokens"]) == 80, legacy[1]
        assert int(legacy[1]["cached_input_tokens"]) == 20, legacy[1]
        assert int(legacy[1]["output_tokens"]) == 15, legacy[1]
        assert int(legacy[2]["total_tokens"]) == 80, legacy[2]
        assert int(direct[0]["total_tokens"]) == 55, direct[0]

    print("Legacy token_count inherited-history/reset regression test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
