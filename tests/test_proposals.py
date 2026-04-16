"""Unit tests for the in-memory proposal store."""

from __future__ import annotations

import asyncio
import time

import pytest

from fleetdb_mcp.proposals import Proposal, ProposalStore


def _make_proposal(pid: str | None = None, created_at: float | None = None) -> Proposal:
    return Proposal(
        proposal_id=pid or ProposalStore.new_id(),
        actor="test-client",
        sql="UPDATE vehicles SET status='retired' WHERE vehicle_id=1",
        kind="UPDATE",
        reason="test",
        explain_plan=[],
        estimated_rows=1,
        created_at=created_at if created_at is not None else time.time(),
    )


class TestProposalStore:
    async def test_add_and_get(self):
        store = ProposalStore(ttl_seconds=60)
        p = _make_proposal()
        await store.add(p)

        got = await store.get(p.proposal_id)
        assert got is not None
        assert got.proposal_id == p.proposal_id

    async def test_pop_removes(self):
        store = ProposalStore(ttl_seconds=60)
        p = _make_proposal()
        await store.add(p)

        popped = await store.pop(p.proposal_id)
        assert popped is not None
        assert popped.proposal_id == p.proposal_id

        # A second pop returns None.
        again = await store.pop(p.proposal_id)
        assert again is None

    async def test_get_missing_returns_none(self):
        store = ProposalStore(ttl_seconds=60)
        assert await store.get("does-not-exist") is None

    async def test_ttl_expiration(self):
        store = ProposalStore(ttl_seconds=60)
        # Manually create a proposal that "was created" 120s ago.
        p = _make_proposal(created_at=time.time() - 120)
        await store.add(p)

        assert await store.get(p.proposal_id) is None

    async def test_list_excludes_expired(self):
        store = ProposalStore(ttl_seconds=60)
        fresh = _make_proposal()
        stale = _make_proposal(created_at=time.time() - 120)

        await store.add(fresh)
        await store.add(stale)

        all_props = await store.list_all()
        ids = {p.proposal_id for p in all_props}
        assert fresh.proposal_id in ids
        assert stale.proposal_id not in ids

    async def test_delete(self):
        store = ProposalStore(ttl_seconds=60)
        p = _make_proposal()
        await store.add(p)

        assert await store.delete(p.proposal_id) is True
        assert await store.delete(p.proposal_id) is False

    async def test_concurrent_pop_is_exclusive(self):
        """Two concurrent pops for the same id: exactly one succeeds."""
        store = ProposalStore(ttl_seconds=60)
        p = _make_proposal()
        await store.add(p)

        results = await asyncio.gather(
            store.pop(p.proposal_id),
            store.pop(p.proposal_id),
            store.pop(p.proposal_id),
        )
        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1

    def test_new_id_is_uuid_like(self):
        pid = ProposalStore.new_id()
        # UUID4 string: 8-4-4-4-12 hex chars
        assert len(pid) == 36
        assert pid.count("-") == 4
