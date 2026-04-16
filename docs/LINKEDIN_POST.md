# LinkedIn Post

## Recommended Version

Today I took my first real step into MCP by building a FleetDB MCP server that connects Claude to a PostgreSQL fleet database.

What made it click for me was seeing it work end-to-end:

- seed a realistic fleet database
- connect it to Claude Desktop
- ask questions like "List the tables" or "Average trip distance by vehicle"
- get back real answers from a live database

The part I cared most about was safety.

I did not want to expose a raw `execute_sql()` write tool and hope the model behaved.
So writes go through a two-step flow instead:

1. `propose_write(sql, reason)`
2. `confirm_write(proposal_id)`

That means every write is previewed first, validated, and then executed with an audit log.

I also learned a very practical MCP lesson:
tools and resources should be designed differently.
Queries and actions belong in tools.
Schema and audit history are better exposed as resources.

Stack:
- MCP Python SDK (FastMCP)
- PostgreSQL + psycopg 3
- sqlparse
- Claude Desktop

The best part was the moment Claude could actually inspect the schema and answer analytical questions from the fleet dataset. That was the point where MCP stopped feeling like a concept and started feeling useful.

Repo: github.com/Yashwinn17/fleetdb-mcp-server

#MCP #ModelContextProtocol #Claude #PostgreSQL #Python #AIEngineering #AgenticAI

---

## Shorter Version

Today I stepped into MCP by building a FleetDB MCP server that connects Claude to a PostgreSQL database.

I seeded a fleet dataset, wired the server into Claude Desktop, and tested prompts like:

- "List the tables"
- "Describe the vehicles table"
- "Average trip distance by vehicle"

The main design choice: writes are not exposed as raw SQL execution.
They go through a two-step confirmation flow with validation and audit logging.

That made MCP feel much more practical to me: not just "LLMs calling tools," but a safe pattern for connecting models to real systems.

Repo: github.com/Yashwinn17/fleetdb-mcp-server

#MCP #Claude #PostgreSQL #Python #AgenticAI

---

## Image Recommendation

Use the Claude Desktop screenshot that shows:

- the `fleetdb` connector visible
- the "Average Trip Distance by Vehicle" result
- enough of the table to prove the query is real

That is stronger than a pure architecture diagram because it proves the system actually works.
