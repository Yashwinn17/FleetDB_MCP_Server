"""End-to-end tests using FastMCP's in-memory client.

These require a live Postgres instance with the schema loaded. Skipped
automatically if the DB is unreachable.

Run:
    docker compose up -d
    python -m fleetdb_mcp.seed
    pytest tests/test_integration.py -v
"""

from __future__ import annotations

import json
import os

import psycopg
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from fleetdb_mcp.config import load_settings
from fleetdb_mcp.server import mcp


def _db_reachable() -> bool:
    try:
        with psycopg.connect(load_settings().database_url, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="Postgres not reachable — run `docker compose up -d` first",
)


async def _parse_tool_result(result) -> dict:
    """FastMCP tool results come back as TextContent blocks — the first one
    contains the JSON payload our tools return."""
    assert result.content, "tool returned no content"
    block = result.content[0]
    text = getattr(block, "text", None)
    assert text is not None, f"unexpected content block: {block}"
    return json.loads(text)


class TestReadTools:
    async def test_list_tables(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool("list_tables", {})
            payload = await _parse_tool_result(res)
            assert "tables" in payload
            # Our seed data guarantees these four exist.
            for t in ("vehicles", "drivers", "trips", "maintenance_events"):
                assert t in payload["tables"]

    async def test_describe_table(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool("describe_table", {"table_name": "vehicles"})
            payload = await _parse_tool_result(res)
            cols = [c["column_name"] for c in payload["columns"]]
            assert "vin" in cols
            assert payload["primary_key"] == ["vehicle_id"]

    async def test_query_read_only_basic(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool(
                "query_read_only",
                {"sql": "SELECT COUNT(*) AS n FROM vehicles", "limit": 10},
            )
            payload = await _parse_tool_result(res)
            assert payload["row_count"] == 1
            assert payload["rows"][0]["n"] >= 0

    async def test_query_read_only_rejects_update(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool(
                "query_read_only",
                {"sql": "UPDATE vehicles SET status='x' WHERE vehicle_id=1", "limit": 10},
            )
            payload = await _parse_tool_result(res)
            assert "error" in payload
            assert "SELECT" in payload["error"]


class TestWriteGate:
    async def test_propose_then_confirm(self):
        """Full round trip: propose → confirm → row actually updated."""
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()

            # Grab a vehicle to target so the test doesn't depend on id=1.
            probe = await client.call_tool(
                "query_read_only",
                {"sql": "SELECT vehicle_id, status FROM vehicles ORDER BY vehicle_id LIMIT 1"},
            )
            probe_payload = await _parse_tool_result(probe)
            target_id = probe_payload["rows"][0]["vehicle_id"]

            # Propose
            res = await client.call_tool(
                "propose_write",
                {
                    "sql": f"UPDATE vehicles SET status='maintenance' WHERE vehicle_id={target_id}",
                    "reason": "integration test",
                },
            )
            prop = await _parse_tool_result(res)
            assert "proposal_id" in prop
            assert prop["kind"] == "UPDATE"

            # Confirm
            res = await client.call_tool(
                "confirm_write", {"proposal_id": prop["proposal_id"]}
            )
            confirmed = await _parse_tool_result(res)
            assert confirmed["executed"] is True
            assert confirmed["rows_affected"] == 1
            assert confirmed["audit_id"] is not None

    async def test_propose_rejects_update_without_where(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool(
                "propose_write",
                {"sql": "UPDATE vehicles SET status='retired'", "reason": "no where"},
            )
            payload = await _parse_tool_result(res)
            assert "error" in payload
            assert "WHERE" in payload["error"]

    async def test_confirm_rejects_unknown_proposal(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.call_tool(
                "confirm_write", {"proposal_id": "00000000-0000-0000-0000-000000000000"}
            )
            payload = await _parse_tool_result(res)
            assert "error" in payload

    async def test_cancel_proposal(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()

            probe = await client.call_tool(
                "query_read_only",
                {"sql": "SELECT vehicle_id FROM vehicles ORDER BY vehicle_id LIMIT 1"},
            )
            probe_payload = await _parse_tool_result(probe)
            target_id = probe_payload["rows"][0]["vehicle_id"]

            res = await client.call_tool(
                "propose_write",
                {
                    "sql": f"UPDATE vehicles SET status='lost' WHERE vehicle_id={target_id}",
                    "reason": "will be cancelled",
                },
            )
            prop = await _parse_tool_result(res)

            res = await client.call_tool(
                "cancel_proposal", {"proposal_id": prop["proposal_id"]}
            )
            payload = await _parse_tool_result(res)
            assert payload["cancelled"] is True

            # Confirming after cancel fails.
            res = await client.call_tool(
                "confirm_write", {"proposal_id": prop["proposal_id"]}
            )
            after = await _parse_tool_result(res)
            assert "error" in after


class TestResources:
    async def test_schema_resource(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.read_resource("schema://tables")
            assert res.contents
            text = res.contents[0].text
            data = json.loads(text)
            assert "tables" in data
            assert "vehicles" in data["tables"]

    async def test_audit_resource(self):
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            await client.initialize()
            res = await client.read_resource("audit://recent")
            assert res.contents
            # Should be a JSON list (maybe empty on fresh DB).
            json.loads(res.contents[0].text)
