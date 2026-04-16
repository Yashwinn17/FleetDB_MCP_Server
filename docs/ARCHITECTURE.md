# Architecture

This document goes one level deeper than the README — it explains *why* each
design decision was made, what the alternatives were, and what the
trade-offs look like in production.

## Protocol layer

The server speaks the Model Context Protocol. MCP is a JSON-RPC-based protocol
for exposing tools, resources, and prompts to LLM clients.

- **Tools** — side-effecting (or at least non-trivial) function calls. Clients
  invoke them; the server returns a result. Analogous to POST endpoints.
- **Resources** — named, read-only data handles that clients can fetch into
  their context. Analogous to GET endpoints.
- **Prompts** — reusable, parameterized prompt templates. Clients surface
  them to the user as slash-commands or saved queries.

This server uses all three primitives deliberately. The audit log is a
**resource**, not a tool, because it is pure read-only data — modelling it as
a resource lets MCP clients cache it and surface it in their UI as context
the user can browse.

## Transport

Default: stdio. The server is spawned as a subprocess by the MCP client and
speaks JSON-RPC over stdin/stdout. This is how Claude Desktop and Claude Code
launch MCP servers — zero network, zero auth, zero exposure.

Optional: `streamable-http` for remote deployments. The FastMCP framework
handles both transports behind the same decorators — no code changes required.

The critical stdio rule: **never write to stdout**. stdout is the JSON-RPC
stream; any stray `print()` corrupts the protocol and the server dies
mysteriously. All logging goes to stderr, configured in `cli.py`.

## Database layer

Async psycopg 3 with `AsyncConnectionPool`. Chosen over SQLAlchemy for:

- Lower overhead. This server is a thin proxy; an ORM is dead weight.
- Explicit SQL everywhere. There's no place for an LLM to smuggle a
  method call through a relationship traversal.
- `dict_row` factory gives us JSON-shaped rows out of the box.

The pool is opened lazily on first tool call. That matters for stdio:
importing the package must not open network sockets, because MCP clients
probe the server on boot.

### Statement timeout

Every connection sets `statement_timeout` at session level before any
user SQL runs. A runaway `SELECT ... JOIN ...` on bad data will be killed
by Postgres after `MCP_QUERY_TIMEOUT_MS` milliseconds.

### Row limit

`query_read_only` wraps the user's SELECT in `SELECT * FROM (...) LIMIT n+1`.
Fetching n+1 rows lets us set `truncated: true` when appropriate without
running the query twice.

## SQL safety

Two validators live in `sql_safety.py`, both built on `sqlparse`:

- `validate_read_only` — accepts a single SELECT. Rejects any DDL, any write,
  and a blocklist of dangerous keywords (COPY, SET, VACUUM, EXECUTE, ...).
- `validate_write` — accepts a single INSERT, UPDATE, or DELETE. UPDATE and
  DELETE must contain a WHERE clause.

### Why sqlparse and not regex

Regex fails on two common inputs:

```sql
SELECT 'please UPDATE me' AS note     -- naive regex flags "UPDATE"
/* UPDATE vehicles SET ... */ SELECT 1  -- naive regex flags the comment
```

`sqlparse` tokenizes the statement and reports the *first DML token* —
`SELECT` in both cases. It also handles `WITH ... SELECT`, mixed case,
trailing semicolons, and comment-embedded writes correctly.

### Single-statement enforcement

Both validators call `sqlparse.parse` and reject anything that splits into
more than one non-empty statement. That kills the `; DROP TABLE` class of
attack before it ever reaches Postgres.

## Two-phase write gate

A single `execute_write(sql)` tool would be a disaster. An LLM sees
"I have a tool that takes SQL and runs it" and sooner or later it will run
`UPDATE vehicles SET status='retired'` without a WHERE clause. Or worse.

The two-phase gate forces a pause:

```
Phase 1   propose_write(sql, reason)
         → validate, EXPLAIN, store as pending proposal
         → return { proposal_id, preview, estimated_rows, explain_plan }

Phase 2   confirm_write(proposal_id)
         → look up proposal, execute in a transaction, write audit
         → return { executed, rows_affected, audit_id }
```

Between the two calls, a human operator (or an MCP client UI like Claude
Desktop) reviews the preview. This is the same pattern Anthropic uses for
computer-use confirmations — expose the decision, do not automate it.

### EXPLAIN preview

Phase 1 runs `EXPLAIN (FORMAT JSON)` inside a rolled-back transaction. We
get a plan and an estimated row count without touching data. The estimate
is Postgres's, derived from stats — accurate enough to catch the obvious
mistakes ("you're about to update 9,842 rows, are you sure?").

### Proposal TTL

Proposals expire after 5 minutes by default (`MCP_PROPOSAL_TTL_S`). This
caps the blast radius of a proposal that gets generated and forgotten.
Every store access evicts expired entries first — we don't need a background
sweeper.

### Atomic audit

Phase 2 runs the user's SQL and the audit INSERT in the *same* transaction.
There is no outcome where a write commits but the audit fails: either
both commit or both roll back. If the user write fails, we open a *second*
transaction just to log the failure, so failed attempts leave a trail too.

## Audit log

Structured log table: `actor`, `tool_name`, `proposal_id`, `sql_text`,
`reason`, `rows_affected`, `success`, `error_message`, `occurred_at`.

The `actor` field is the MCP client identifier — set per client via the
`MCP_ACTOR` env var at launch time. Running the same server for two different
MCP clients gives you two different actor strings in the log, which is
exactly what you want for accountability.

The audit log is exposed as two resources:

- `audit://recent` — last 50 entries
- `audit://by-actor/{actor}` — filtered by actor

These are resources, not tools, because they are pure reads. MCP clients
can surface them in their UI without the LLM having to "decide" to fetch them.

## Things this server deliberately does not do

- **No LLM calls inside the server.** The LLM is whatever the client
  connects with. MCP is a separation-of-concerns protocol — mixing an LLM
  into the server defeats the point.
- **No caching.** Postgres is fast enough for this workload. Caching
  would add staleness bugs and an invalidation problem.
- **No per-user RLS.** Out of scope for v0. Add via `SET ROLE` on
  checkout if needed.
- **No schema auto-discovery at startup.** We read schema on demand via
  `information_schema`. Cold-start is faster and we pick up DDL changes
  without restart.

## Deployment notes

For a single-developer or single-team deployment: stdio + docker-compose is
plenty. The server starts in ~200ms, connects on first use, lives as long
as the MCP client.

For a shared deployment: switch to `streamable-http`, put a reverse proxy
in front, add OAuth (FastMCP supports it), and move the proposal store to
Redis so multiple server instances share state.

## Testing strategy

Three layers, in descending order of coverage-to-cost:

1. **SQL validator unit tests** — no DB, runs in milliseconds, covers the
   riskiest code in the repo.
2. **Proposal store unit tests** — no DB, covers TTL + concurrency.
3. **Integration tests** — live Postgres via FastMCP's in-memory client.
   Exercises the whole tool surface without needing Claude Desktop or a
   real MCP client. Skipped automatically if the DB is unreachable.
