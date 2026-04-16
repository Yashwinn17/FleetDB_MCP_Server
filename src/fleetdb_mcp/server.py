"""FleetDB MCP Server.

Exposes read tools, write tools (two-phase gate), resources, and prompts.

Design notes
------------
- Built on FastMCP (official MCP Python SDK high-level API).
- The server is a module-level singleton so `fleetdb-mcp` can `mcp.run()` directly.
- Global `_state` holds the DB and the proposal store. `_init_state()` is idempotent.
- All DB work is async via psycopg 3. Tool functions are async.
- Tool return shapes are plain JSON-able dicts — the MCP SDK serializes them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

from .audit import write_audit
from .config import Settings, load_settings
from .db import Database
from .proposals import Proposal, ProposalStore
from .sql_safety import SQLValidationError, validate_read_only, validate_write

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# Shared state
# ------------------------------------------------------------


@dataclass
class _State:
    settings: Settings
    db: Database
    proposals: ProposalStore


_state: _State | None = None


def _init_state() -> _State:
    global _state
    if _state is None:
        settings = load_settings()
        _state = _State(
            settings=settings,
            db=Database(settings),
            proposals=ProposalStore(settings.proposal_ttl_seconds),
        )
    return _state


# ------------------------------------------------------------
# Server
# ------------------------------------------------------------

mcp = FastMCP(
    name="fleetdb",
    instructions=(
        "FleetDB MCP Server. Exposes a PostgreSQL fleet-management database. "
        "Use read tools for analysis. For writes, call propose_write first, "
        "review the preview, then call confirm_write with the returned proposal_id. "
        "Schemas and audit logs are available as resources."
    ),
)


# ============================================================
# READ TOOLS
# ============================================================


@mcp.tool()
async def list_tables() -> dict[str, Any]:
    """List all user tables in the database.

    Returns a list of table names in the public schema. Use describe_table
    to drill into a specific table.
    """
    state = _init_state()
    rows = await state.db.fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return {"tables": [r["table_name"] for r in rows]}


@mcp.tool()
async def describe_table(table_name: str) -> dict[str, Any]:
    """Return the schema of a single table.

    Args:
        table_name: The name of the table (in the `public` schema).

    Returns column names, types, nullability, defaults, and primary keys.
    """
    state = _init_state()
    cols = await state.db.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    if not cols:
        return {"error": f"table not found: {table_name}"}

    pks = await state.db.fetch_all(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (table_name,),
    )
    return {
        "table": table_name,
        "columns": cols,
        "primary_key": [p["column_name"] for p in pks],
    }


@mcp.tool()
async def query_read_only(sql: str, limit: int = 100) -> dict[str, Any]:
    """Execute a read-only SELECT and return the rows.

    Args:
        sql: A single SELECT statement. UPDATEs, DDL, COPY, etc. are rejected.
        limit: Max rows to return (capped at the server's configured max_rows).

    The query runs with a server-side statement timeout. Row counts beyond
    the limit are reported via `truncated`.
    """
    state = _init_state()
    try:
        validated = validate_read_only(sql)
    except SQLValidationError as e:
        return {"error": f"validation failed: {e}"}

    capped = max(1, min(limit, state.settings.max_rows))

    # We wrap the user's SELECT in a subquery with LIMIT n+1 so we can detect
    # truncation without re-running the query.
    wrapped = f"SELECT * FROM ({validated.normalized_sql}) AS _q LIMIT {capped + 1}"

    try:
        rows = await state.db.fetch_all(wrapped)
    except Exception as e:
        return {"error": f"execution failed: {e.__class__.__name__}: {e}"}

    truncated = len(rows) > capped
    rows = rows[:capped]
    return {
        "rows": _jsonable_rows(rows),
        "row_count": len(rows),
        "truncated": truncated,
        "limit_applied": capped,
    }


@mcp.tool()
async def get_table_stats(table_name: str) -> dict[str, Any]:
    """Return row count and size for a single table.

    Args:
        table_name: The name of the table (in the `public` schema).
    """
    state = _init_state()

    # Use an identifier-safe pattern: look up the table first via information_schema,
    # then run a fully-controlled query. We never interpolate the name into SQL.
    exists = await state.db.fetch_one(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    if not exists:
        return {"error": f"table not found: {table_name}"}

    # pg_class.relname is a safer lookup; we pass table_name as a parameter
    # and format via psycopg's Identifier-style quoting below.
    stats_row = await state.db.fetch_one(
        """
        SELECT
            c.reltuples::BIGINT              AS approx_row_count,
            pg_total_relation_size(c.oid)    AS total_bytes,
            pg_relation_size(c.oid)          AS table_bytes,
            s.last_analyze,
            s.last_autoanalyze
        FROM pg_class c
        LEFT JOIN pg_stat_user_tables s ON s.relname = c.relname
        WHERE c.relname = %s
          AND c.relnamespace = 'public'::regnamespace
        """,
        (table_name,),
    )
    return {"table": table_name, "stats": _jsonable_row(stats_row or {})}


# ============================================================
# WRITE TOOLS (two-phase gate)
# ============================================================


@mcp.tool()
async def propose_write(sql: str, reason: str) -> dict[str, Any]:
    """PHASE 1: Validate a write and return a proposal the user can approve.

    Args:
        sql: A single INSERT, UPDATE, or DELETE. UPDATEs and DELETEs must
            have a WHERE clause.
        reason: A short human-readable explanation of why this write should
            be performed. Will be stored in the audit log.

    Returns a proposal_id, an EXPLAIN plan, and an estimated row count.
    The write does NOT execute — call confirm_write(proposal_id) to execute.
    """
    state = _init_state()

    try:
        validated = validate_write(sql)
    except SQLValidationError as e:
        return {"error": f"validation failed: {e}"}

    if not reason or not reason.strip():
        return {"error": "a non-empty 'reason' is required for writes"}

    # Run EXPLAIN inside a transaction we immediately roll back so we get an
    # impact estimate without actually writing.
    try:
        async with state.db.connection() as conn:
            async with conn.transaction(force_rollback=True):
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"EXPLAIN (FORMAT JSON) {validated.normalized_sql}"
                    )
                    plan_row = await cur.fetchone()
    except Exception as e:
        return {"error": f"EXPLAIN failed: {e.__class__.__name__}: {e}"}

    plan_json = plan_row["QUERY PLAN"] if plan_row else []
    estimated_rows = _estimate_rows(plan_json)

    proposal = Proposal(
        proposal_id=ProposalStore.new_id(),
        actor=state.settings.actor,
        sql=validated.normalized_sql,
        kind=validated.kind,
        reason=reason.strip(),
        explain_plan=plan_json,
        estimated_rows=estimated_rows,
    )
    await state.proposals.add(proposal)

    return {
        "proposal_id": proposal.proposal_id,
        "kind": proposal.kind,
        "sql": proposal.sql,
        "reason": proposal.reason,
        "estimated_rows": estimated_rows,
        "explain_plan": plan_json,
        "expires_at": proposal.expires_at,
        "next_step": (
            f"Review the preview. When approved, call "
            f"confirm_write(proposal_id='{proposal.proposal_id}')."
        ),
    }


@mcp.tool()
async def confirm_write(proposal_id: str) -> dict[str, Any]:
    """PHASE 2: Execute a previously proposed write.

    Args:
        proposal_id: The id returned from propose_write.

    Executes the stored SQL in a transaction and writes an atomic audit row.
    If execution fails, the audit row still records the attempt.
    """
    state = _init_state()

    proposal = await state.proposals.pop(proposal_id)
    if proposal is None:
        return {"error": f"proposal not found or expired: {proposal_id}"}

    async with state.db.connection() as conn:
        # Both the user write and the audit write live in one transaction.
        # If the user write raises, we roll back and log the failure separately.
        rows_affected: int | None = None
        success = False
        err: str | None = None
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(proposal.sql)
                    rows_affected = cur.rowcount

                audit_row = await write_audit(
                    conn,
                    actor=proposal.actor,
                    tool_name="confirm_write",
                    proposal_id=proposal.proposal_id,
                    sql_text=proposal.sql,
                    reason=proposal.reason,
                    rows_affected=rows_affected,
                    success=True,
                    error_message=None,
                )
                success = True
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
            # Log the failure in its own transaction.
            try:
                async with conn.transaction():
                    audit_row = await write_audit(
                        conn,
                        actor=proposal.actor,
                        tool_name="confirm_write",
                        proposal_id=proposal.proposal_id,
                        sql_text=proposal.sql,
                        reason=proposal.reason,
                        rows_affected=None,
                        success=False,
                        error_message=err,
                    )
            except Exception:
                logger.exception("failed to write failure audit row")
                audit_row = {"audit_id": None, "occurred_at": None}

    if success:
        return {
            "executed": True,
            "proposal_id": proposal.proposal_id,
            "rows_affected": rows_affected,
            "audit_id": audit_row.get("audit_id"),
        }
    return {
        "executed": False,
        "proposal_id": proposal.proposal_id,
        "error": err,
        "audit_id": audit_row.get("audit_id"),
    }


@mcp.tool()
async def list_pending_proposals() -> dict[str, Any]:
    """List all write proposals that haven't been confirmed or expired."""
    state = _init_state()
    props = await state.proposals.list_all()
    return {"proposals": [p.to_dict() for p in props]}


@mcp.tool()
async def cancel_proposal(proposal_id: str) -> dict[str, Any]:
    """Cancel a pending write proposal without executing it.

    Args:
        proposal_id: The id returned from propose_write.
    """
    state = _init_state()
    ok = await state.proposals.delete(proposal_id)
    return {"cancelled": ok, "proposal_id": proposal_id}


# ============================================================
# RESOURCES
# ============================================================


@mcp.resource("schema://tables")
async def resource_all_tables() -> str:
    """Complete database schema as JSON."""
    state = _init_state()
    rows = await state.db.fetch_all(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["table_name"], []).append(
            {
                "column": r["column_name"],
                "type": r["data_type"],
                "nullable": r["is_nullable"] == "YES",
            }
        )
    return json.dumps({"tables": grouped}, indent=2)


@mcp.resource("schema://table/{name}")
async def resource_single_table(name: str) -> str:
    """Schema of a single table."""
    result = await describe_table.fn(name)  # type: ignore[attr-defined]
    return json.dumps(result, indent=2, default=str)


@mcp.resource("audit://recent")
async def resource_recent_audit() -> str:
    """Last 50 audit log entries."""
    state = _init_state()
    rows = await state.db.fetch_all(
        """
        SELECT audit_id, occurred_at, actor, tool_name, rows_affected,
               success, reason, error_message
        FROM mcp_audit_log
        ORDER BY occurred_at DESC
        LIMIT 50
        """
    )
    return json.dumps(_jsonable_rows(rows), indent=2, default=str)


@mcp.resource("audit://by-actor/{actor}")
async def resource_audit_by_actor(actor: str) -> str:
    """Audit log entries for a specific actor."""
    state = _init_state()
    rows = await state.db.fetch_all(
        """
        SELECT audit_id, occurred_at, tool_name, rows_affected,
               success, reason, error_message
        FROM mcp_audit_log
        WHERE actor = %s
        ORDER BY occurred_at DESC
        LIMIT 100
        """,
        (actor,),
    )
    return json.dumps(_jsonable_rows(rows), indent=2, default=str)


# ============================================================
# PROMPTS
# ============================================================


@mcp.prompt()
def analyze_fleet() -> str:
    """Ask the LLM to pull the schema resource and surface fleet KPIs."""
    return (
        "You have access to the FleetDB MCP server. "
        "Start by reading the `schema://tables` resource to understand the data model. "
        "Then use `query_read_only` to compute these KPIs:\n"
        "  1. Total active vehicles by fuel type\n"
        "  2. Top 5 vehicles by maintenance cost in the last 90 days\n"
        "  3. Average trip distance per driver over the last 30 days\n"
        "  4. Any vehicles whose license is within 30 days of expiring\n"
        "Return the results in a concise executive summary with numbers and one-line insights."
    )


@mcp.prompt()
def maintenance_report(window_days: int = 30) -> str:
    """Generate a structured maintenance report prompt."""
    return (
        f"Using the FleetDB MCP server, produce a maintenance report for the "
        f"last {window_days} days. Cover: total events, total cost, total "
        f"downtime hours, breakdown by event_type, and a ranked list of the "
        f"five vehicles with the most events. Use `query_read_only` and cite "
        f"the SQL you ran."
    )


# ============================================================
# Helpers
# ============================================================


def _estimate_rows(plan_json: list[dict[str, Any]]) -> int | None:
    """Pull the top-level plan row estimate out of an EXPLAIN FORMAT JSON payload."""
    if not plan_json:
        return None
    try:
        return int(plan_json[0]["Plan"]["Plan Rows"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_jsonable_row(r) for r in rows]


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert Decimal / datetime / etc. to JSON-friendly primitives."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):  # datetime, date, time
            out[k] = v.isoformat()
        elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
            # Decimal
            out[k] = float(v)
        else:
            out[k] = v
    return out
