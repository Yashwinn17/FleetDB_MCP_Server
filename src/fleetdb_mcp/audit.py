"""Audit log writer.

Every confirmed write (successful or failed) gets a row in mcp_audit_log.
The audit write is committed in the *same transaction* as the user write
so there is no way to execute a write without also recording it.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


_INSERT_AUDIT = """
INSERT INTO mcp_audit_log (
    actor, tool_name, proposal_id, sql_text,
    reason, rows_affected, success, error_message
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
RETURNING audit_id, occurred_at
"""


async def write_audit(
    conn: psycopg.AsyncConnection[Any],
    *,
    actor: str,
    tool_name: str,
    proposal_id: str | None,
    sql_text: str,
    reason: str | None,
    rows_affected: int | None,
    success: bool,
    error_message: str | None,
) -> dict[str, Any]:
    """Insert an audit row using the supplied connection.

    Pass the same connection used to execute the user's write so that the
    audit row commits atomically with the write itself.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            _INSERT_AUDIT,
            (
                actor,
                tool_name,
                proposal_id,
                sql_text,
                reason,
                rows_affected,
                success,
                error_message,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # pragma: no cover — RETURNING always gives us a row
        raise RuntimeError("audit insert returned no row")
    logger.info(
        "audit_write",
        extra={
            "audit_id": row["audit_id"],
            "actor": actor,
            "tool": tool_name,
            "success": success,
        },
    )
    return row
