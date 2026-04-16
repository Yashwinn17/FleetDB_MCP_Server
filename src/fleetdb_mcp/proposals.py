"""In-memory store for pending write proposals.

A proposal captures a validated write SQL statement plus its EXPLAIN-estimated
impact. It lives for `proposal_ttl_seconds`, then expires. A client goes:

    propose_write(sql, reason)  →  proposal_id
    confirm_write(proposal_id)  →  executes + audits

Thread-safe via asyncio.Lock; good for single-process deployments. For a
multi-process deployment, swap for Redis.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Proposal:
    """A pending write, waiting for confirmation."""

    proposal_id: str
    actor: str
    sql: str
    kind: str                 # "INSERT" | "UPDATE" | "DELETE"
    reason: str
    explain_plan: list[dict[str, Any]]
    estimated_rows: int | None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0   # set by the store

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "actor": self.actor,
            "sql": self.sql,
            "kind": self.kind,
            "reason": self.reason,
            "estimated_rows": self.estimated_rows,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ProposalStore:
    """In-memory TTL store for write proposals."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._by_id: dict[str, Proposal] = {}
        self._lock = asyncio.Lock()

    async def add(self, proposal: Proposal) -> Proposal:
        async with self._lock:
            proposal.expires_at = proposal.created_at + self._ttl
            self._by_id[proposal.proposal_id] = proposal
            self._evict_expired_locked()
            return proposal

    async def get(self, proposal_id: str) -> Proposal | None:
        async with self._lock:
            self._evict_expired_locked()
            return self._by_id.get(proposal_id)

    async def pop(self, proposal_id: str) -> Proposal | None:
        """Remove and return a proposal atomically — used by confirm_write."""
        async with self._lock:
            self._evict_expired_locked()
            return self._by_id.pop(proposal_id, None)

    async def list_all(self) -> list[Proposal]:
        async with self._lock:
            self._evict_expired_locked()
            return list(self._by_id.values())

    async def delete(self, proposal_id: str) -> bool:
        async with self._lock:
            return self._by_id.pop(proposal_id, None) is not None

    # ------------------------------------------------------------

    def _evict_expired_locked(self) -> None:
        """Caller must hold the lock."""
        now = time.time()
        expired = [pid for pid, p in self._by_id.items() if p.expires_at < now]
        for pid in expired:
            del self._by_id[pid]

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())
