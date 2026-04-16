"""Configuration for the FleetDB MCP server.

All config is loaded from environment variables. `.env` is auto-loaded at import time
so that `fleetdb-mcp` works out of the box when launched by an MCP client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Server-wide settings.

    Attributes
    ----------
    database_url:
        Postgres DSN. Defaults to the local docker-compose instance.
    actor:
        Identifier recorded in the audit log for every write. Should be set
        per client (e.g. "claude-desktop", "claude-code", "langgraph-agent").
    max_rows:
        Hard cap on rows returned by query_read_only. Clients can request
        fewer but never more.
    query_timeout_ms:
        Statement timeout applied to every query via Postgres `SET LOCAL statement_timeout`.
    proposal_ttl_seconds:
        How long a write proposal lives before confirm_write will reject it.
    """

    database_url: str
    actor: str
    max_rows: int
    query_timeout_ms: int
    proposal_ttl_seconds: int


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://fleet:fleet@localhost:5432/fleetdb",
        ),
        actor=os.getenv("MCP_ACTOR", "unknown-client"),
        max_rows=int(os.getenv("MCP_MAX_ROWS", "500")),
        query_timeout_ms=int(os.getenv("MCP_QUERY_TIMEOUT_MS", "5000")),
        proposal_ttl_seconds=int(os.getenv("MCP_PROPOSAL_TTL_S", "300")),
    )
