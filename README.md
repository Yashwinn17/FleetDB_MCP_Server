# FleetDB MCP Server

> **Day 25 of the 84-Day Agentic AI Engineer Blueprint**
> Author: [Yashwin Vasanth Srinivasan](https://www.linkedin.com/in/yashwin-vasanth) · [@Yashwinn17](https://github.com/Yashwinn17)

A production-grade **Model Context Protocol (MCP) server** that exposes a PostgreSQL fleet-management database to any MCP-compatible client (Claude Desktop, Claude Code, LangGraph agents, CrewAI agents, custom clients). Queries, analysis, and controlled writes happen via natural language — with safety rails at every layer.

## Why this exists

MCP is the emerging standard for AI tool integration. This repo demonstrates:

1. **Tool exposure** — both read-only and write operations as MCP tools
2. **Resource sharing** — schemas and audit logs exposed as MCP resources
3. **Safety-first write path** — a two-phase confirmation gate, not raw SQL-write endpoints
4. **Auditability** — every mutating operation is logged to `mcp_audit_log` and exposed as a resource

Built to be the kind of MCP server you'd actually want to run in front of a real database.

## Architecture

```
┌──────────────┐   ┌──────────────┐   ┌──────────────────┐
│ Claude Desk. │   │ Claude Code  │   │ LangGraph agent  │
└──────┬───────┘   └──────┬───────┘   └────────┬─────────┘
       │       stdio / JSON-RPC                │
       └──────────────┬────────────────────────┘
                      ▼
         ┌──────────────────────────────┐
         │  FleetDB MCP Server (FastMCP)│
         │ ┌────────────┐ ┌───────────┐ │
         │ │ Read tools │ │Write tools│ │
         │ │ (SELECT)   │ │ (2-phase) │ │
         │ └────────────┘ └───────────┘ │
         │ ┌──────────────────────────┐ │
         │ │       Resources          │ │
         │ │  schema://, audit://     │ │
         │ └──────────────────────────┘ │
         │  Safety: sqlparse, row cap,  │
         │  query timeout, audit writer │
         └──────────────┬───────────────┘
                        ▼
                  ┌───────────┐
                  │PostgreSQL │
                  └───────────┘
```

## Capabilities

### Read tools (no confirmation required)

| Tool | Purpose |
|---|---|
| `list_tables()` | Enumerate all user tables in the `public` schema |
| `describe_table(table_name)` | Column names, types, nullability, primary keys |
| `query_read_only(sql, limit)` | Execute a `SELECT` statement — validated to be read-only, row-capped, timed out |
| `get_table_stats(table_name)` | Row count, approximate size, last-analyzed timestamp |

### Write tools (two-phase confirmation gate)

| Tool | Phase | Purpose |
|---|---|---|
| `propose_write(sql, reason)` | 1 | Validates SQL, runs `EXPLAIN`, stores a proposal with a UUID and TTL |
| `confirm_write(proposal_id)` | 2 | Executes the stored proposal inside a transaction and logs to audit |
| `list_pending_proposals()` | — | Shows outstanding proposals the client has created |
| `cancel_proposal(proposal_id)` | — | Drops a proposal without executing |

### Resources (MCP-native, read-only)

| URI | Content |
|---|---|
| `schema://tables` | Full DB schema as JSON |
| `schema://table/{name}` | Single-table schema |
| `audit://recent` | Last 50 audit log entries |
| `audit://by-actor/{actor}` | Audit entries filtered by actor name |

### Prompts

| Prompt | Purpose |
|---|---|
| `analyze_fleet` | Pre-built analysis prompt that points a client at the schema resource and asks for KPIs |
| `maintenance_report` | Generates a structured maintenance report prompt |

## The two-phase write gate — why and how

Raw write endpoints against a database are dangerous. An agent that sees `execute_sql(query)` will, sooner or later, run an `UPDATE` without a `WHERE` clause.

This server forces a two-step flow:

```
client → propose_write(sql, reason)
         ← { proposal_id, preview, explain_plan, estimated_rows, expires_at }

(agent or human reviews the preview)

client → confirm_write(proposal_id)
         ← { executed: true, rows_affected, audit_id }
```

Between the two calls:

- The SQL is parsed and checked — only a single statement, must be `INSERT/UPDATE/DELETE`, must have a `WHERE` clause for `UPDATE`/`DELETE`
- `EXPLAIN (FORMAT JSON)` runs inside a rolled-back transaction — gives an estimated row count
- The proposal is stored in-memory (or Redis, configurable) with a 5-minute TTL
- The human-in-the-loop pattern: MCP clients like Claude Desktop will surface the preview and let the user approve

The execution itself happens in a transaction and writes to `mcp_audit_log` atomically.

## Domain: fleet / mobility

The seed schema reflects a real fleet-ops use case:

- `vehicles` — vehicle identity, make/model, VIN, status
- `drivers` — driver identity, license info
- `maintenance_events` — service history, costs, downtime
- `trips` — trip records with origin/destination, distance, fuel

This mirrors the kind of database an early-stage [MobilityOps AI](https://github.com/Yashwinn17) deployment would run. It's also enough complexity to demo non-trivial analytical queries.

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/Yashwinn17/fleetdb-mcp-server.git
cd fleetdb-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Start Postgres (docker)
docker compose up -d

# 3. Seed the database
python -m fleetdb_mcp.seed

# 4. Run the server (stdio — default for MCP clients)
fleetdb-mcp

# OR: run over HTTP for debugging
fleetdb-mcp --transport streamable-http --port 8000
```

If step 3 fails with `password authentication failed for user "fleet"`, your local Postgres on port `5432` is not using the repo's expected credentials. This usually means either:

- another Postgres instance is already bound to `localhost:5432`, or
- the Docker volume was initialized earlier with a different password, so changing `POSTGRES_PASSWORD` in `docker-compose.yml` no longer updates that existing data directory.

For the repo defaults, use:

```bash
DATABASE_URL=postgresql://fleet:fleet@localhost:5432/fleetdb
```

If you want the compose-managed database reset to repo defaults, recreate it from scratch:

```bash
docker compose down -v
docker compose up -d
python -m fleetdb_mcp.seed
```

## Wiring into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "fleetdb": {
      "command": "/absolute/path/to/.venv/bin/fleetdb-mcp",
      "env": {
        "DATABASE_URL": "postgresql://fleet:fleet@localhost:5432/fleetdb",
        "MCP_ACTOR": "claude-desktop"
      }
    }
  }
}
```

Restart Claude Desktop. The `fleetdb` server appears in the tools panel.

## Wiring into Claude Code

```bash
claude mcp add fleetdb /absolute/path/to/.venv/bin/fleetdb-mcp \
  --env DATABASE_URL=postgresql://fleet:fleet@localhost:5432/fleetdb \
  --env MCP_ACTOR=claude-code
```

## Example prompts (once wired in)

- *"What's the average cost of oil changes across the fleet in the last 90 days?"*
- *"Which three vehicles have the worst uptime? Show me their maintenance history."*
- *"Mark vehicle VIN 1HGBH41JXMN109186 as retired."* — this will hit the confirmation gate
- *"Show me the last 10 audit entries."* — uses the `audit://recent` resource

## Testing

```bash
pytest                          # Unit tests (SQL validator, proposal store)
pytest tests/test_integration.py  # Requires a live DB
```

The integration tests use the built-in `FastMCP` in-memory client — no real MCP client required.

## Technical stack

- **MCP Python SDK** (`mcp>=1.27.0`) — `FastMCP` high-level API
- **psycopg[binary]** — async PostgreSQL driver
- **sqlparse** — SQL statement validation (safer than regex)
- **Pydantic** — schema validation on tool inputs/outputs
- **pytest + pytest-asyncio** — test harness

## LLM-Factory footnote

Per the 84-day blueprint, this repo doesn't run an LLM itself — **that's the whole point of MCP.** The LLM is whatever the client connects with. During development I tested against Claude Desktop (Sonnet 4.6) and a small LangGraph harness backed by the LLM factory (Ollama `llama3.1:8b` primary, Groq `llama-3.3-70b-versatile` fallback).

## Roadmap

- [ ] Row-level security / per-actor scoping
- [ ] Read replicas for heavy analytical queries
- [ ] Streaming results for large `SELECT`s (MCP supports this)
- [ ] OTel tracing on every tool call

## License

MIT.
