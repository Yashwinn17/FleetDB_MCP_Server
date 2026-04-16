"""Async PostgreSQL connection pool.

Wraps psycopg_pool.AsyncConnectionPool with helpers that enforce the
query timeout and return dict rows.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings

logger = logging.getLogger(__name__)


class Database:
    """Thin async wrapper around an AsyncConnectionPool.

    The pool is created lazily on first use so that importing this module
    does not open a socket — important for MCP clients that spawn the server
    on-demand over stdio.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool | None = None

    async def start(self) -> None:
        if self._pool is not None:
            return
        # open=False + explicit open() lets us surface connection errors cleanly.
        pool = AsyncConnectionPool(
            conninfo=self._settings.database_url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await pool.open()
        self._pool = pool
        logger.info("postgres pool opened", extra={"dsn_host": _dsn_host(self._settings.database_url)})

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        """Yield a connection with the statement_timeout already applied."""
        if self._pool is None:
            await self.start()
        assert self._pool is not None
        async with self._pool.connection() as conn:
            # SET LOCAL only lives inside a transaction, so we apply the timeout
            # via a session-level SET that lasts for this checked-out connection.
            await conn.execute(
                f"SET statement_timeout = {self._settings.query_timeout_ms}"
            )
            yield conn

    async def fetch_all(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        async with self.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(await cur.fetchall())

    async def fetch_one(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> dict[str, Any] | None:
        async with self.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            if cur.description is None:
                return None
            return await cur.fetchone()


def _dsn_host(dsn: str) -> str:
    """Best-effort host extraction for logging — never logs credentials."""
    try:
        return dsn.split("@", 1)[1].split("/", 1)[0]
    except (IndexError, AttributeError):
        return "unknown"
