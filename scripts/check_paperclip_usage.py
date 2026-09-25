#!/usr/bin/env python3
"""Fixture checks for Paperclip run/account/billing normalization."""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import collect_paperclip_usage as paperclip
import usage_model


def agent(agent_id: str, name: str, adapter: str, model: str) -> dict:
    return {
        "id": agent_id,
        "name": name,
        "role": "engineer",
        "adapterType": adapter,
        "adapterConfig": {"model": model},
        "runtimeConfig": {},
    }


def run(
    run_id: str,
    agent_id: str,
    when: str,
    *,
    connection_id: str = "",
    provider: str = "",
    method: str = "",
    model: str = "",
    tokens: bool = True,
) -> dict:
    ai = {}
    if connection_id:
        ai["connectionId"] = connection_id
    if provider:
        ai["provider"] = provider
    if method:
        ai["method"] = method
    usage = {}
    if tokens:
        usage = {
            "provider": provider,
            "model": model,
            "inputTokens": 100,
            "cachedInputTokens": 50,
            "outputTokens": 25,
        }
    return {
        "id": run_id,
        "agentId": agent_id,
        "status": "succeeded",
        "startedAt": when,
        "finishedAt": when.replace(":00Z", ":30Z"),
        "sessionIdAfter": f"session-{run_id}",
        "contextSnapshot": {
            "aiConnection": ai,
            "issueId": f"task-{run_id}",
            "projectId": "project-1",
        },
        "usageJson": usage,
    }


def main() -> int:
    agents = {
        "atlas": agent("atlas", "Atlas", "codex_local", "gpt-5.6-sol"),
        "dev2": agent("dev2", "Developer 2", "codex_local", "gpt-5.6-sol"),
        "local": agent("local", "Personal Assistant", "opencode_local", "ollama/gpt-oss:20b"),
        "api": agent("api", "API Worker", "opencode_local", "openai/gpt-6-sol"),
    }
    connections = {
        "sub-a": {
            "id": "sub-a", "name": "Codex Main", "provider": "openai",
            "method": "subscription", "status": "active",
        },
        "sub-b": {
            "id": "sub-b", "name": "Codex Second", "provider": "openai",
            "method": "subscription", "status": "active",
        },
        "api-a": {
            "id": "api-a", "name": "OpenAI API", "provider": "openai",
            "method": "api_key", "status": "active",
        },
    }

    fixtures = [
        # Agent Atlas first uses Subscription A and later switches to B.
        run("atlas-a", "atlas", "2026-09-23T00:00:00Z", connection_id="sub-a", provider="openai", method="subscription", model="gpt-5.6-sol"),
        run("atlas-b", "atlas", "2026-09-23T01:00:00Z", connection_id="sub-b", provider="openai", method="subscription", model="gpt-5.6-sol"),
        # A different agent shares Subscription A.
        run("dev2-a", "dev2", "2026-09-23T02:00:00Z", connection_id="sub-a", provider="openai", method="subscription", model="gpt-5.6-sol"),
        # Local OpenCode/Ollama.
        run("local-1", "local", "2026-09-23T03:00:00Z", provider="ollama", model="ollama/gpt-oss:20b"),
        # API-key based run.
        run("api-1", "api", "2026-09-23T04:00:00Z", connection_id="api-a", provider="openai", method="api_key", model="openai/gpt-6-sol"),
        # Run with no token counters remains present.
        run("missing-usage", "atlas", "2026-09-23T05:00:00Z", connection_id="sub-a", provider="openai", method="subscription", model="gpt-5.6-sol", tokens=False),
        # Stable but unresolved connection id must not receive a guessed name.
        run("unknown-account", "atlas", "2026-09-23T06:00:00Z", connection_id="missing-connection", provider="openai", method="subscription", model="gpt-5.6-sol"),
    ]

    normalized = [
        paperclip.normalize_run(item, agents[item["agentId"]], connections, timezone.utc)
        for item in fixtures
    ]
    records = [pair[0] for pair in normalized]
    runs = [pair[1] for pair in normalized]

    by_id = {row["run_id"]: row for row in runs}
    assert by_id["atlas-a"]["account_connection_id"] == "sub-a"
    assert by_id["atlas-b"]["account_connection_id"] == "sub-b"
    assert by_id["dev2-a"]["account_connection_id"] == "sub-a"
    assert by_id["local-1"]["provider"] == "ollama"
    assert by_id["local-1"]["model"] == "gpt-oss:20b"
    assert by_id["local-1"]["billing_mode"] == "local"
    assert by_id["local-1"]["account_display_name"] == "Local Ollama"
    assert by_id["local-1"]["actual_cost_available"] == 1
    assert by_id["local-1"]["actual_provider_cost_usd"] == 0.0
    assert by_id["api-1"]["billing_mode"] == "api"
    assert by_id["missing-usage"]["token_metrics_available"] == 0
    assert by_id["unknown-account"]["account_connection_id"] == "missing-connection"
    assert by_id["unknown-account"]["account_display_name"] == "Unknown account"

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        db = data_dir / "codex_usage.sqlite"
        conn = sqlite3.connect(db)
        try:
            usage_model.ensure_schema(conn)
            usage_model.replace_source_dates(
                conn, usage_model.SOURCE_PAPERCLIP, {"2026-09-23"}, records, runs
            )
            conn.commit()
            # Repeat exactly the same refresh. Stable run keys/date replacement
            # must make the result idempotent.
            usage_model.replace_source_dates(
                conn, usage_model.SOURCE_PAPERCLIP, {"2026-09-23"}, records, runs
            )
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM usage_runs WHERE source_system=?",
                (usage_model.SOURCE_PAPERCLIP,),
            ).fetchone()[0]
            assert count == len(fixtures), count
            atlas_accounts = {
                row[0]
                for row in conn.execute(
                    "SELECT account_connection_id FROM usage_runs WHERE agent_id='atlas'"
                )
            }
            assert {"sub-a", "sub-b"} <= atlas_accounts, atlas_accounts
        finally:
            conn.close()

        # Paperclip source failure is non-fatal and does not remove existing
        # normalized usage.
        args = argparse.Namespace(
            days=1, from_date=None, to_date=None, output_dir=data_dir,
            base_url="http://127.0.0.1:1", company_id="", api_token="",
            timeout=0.05,
        )
        assert paperclip.collect(args) == 0
        conn = sqlite3.connect(db)
        try:
            status = conn.execute(
                "SELECT status FROM source_status WHERE source_system=?",
                (usage_model.SOURCE_PAPERCLIP,),
            ).fetchone()[0]
            remaining = conn.execute(
                "SELECT COUNT(*) FROM usage_runs WHERE source_system=?",
                (usage_model.SOURCE_PAPERCLIP,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert status == "unavailable", status
        assert remaining == len(fixtures), remaining

    print("Paperclip normalization, account switching, local/API, idempotency and outage tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
