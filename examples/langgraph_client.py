"""Example LangGraph agent that uses the FleetDB MCP server as its tool backend.

This ties Day 25 back to the Week 2 LangGraph patterns: the agent is a
simple ReAct loop, but every tool call goes through MCP — meaning the
*same* server powers Claude Desktop, Claude Code, and this agent.

LLM factory: Ollama primary (local, free), Groq Llama 70B fallback (for
reliable tool-calling when the local model is not up to it).

Run:
    # 1. Make sure the MCP server works locally:
    fleetdb-mcp --transport stdio   # in another shell, or let the adapter spawn it

    # 2. Install extra deps:
    pip install langchain-mcp-adapters langgraph langchain-ollama langchain-groq

    # 3. Set env:
    export DATABASE_URL=postgresql://fleet:fleet@localhost:5432/fleetdb
    export MCP_ACTOR=langgraph-agent
    # Optional, for fallback:
    export GROQ_API_KEY=...

    # 4. Run:
    python examples/langgraph_client.py "Which three vehicles have spent the most on maintenance in the last 90 days?"
"""

from __future__ import annotations

import asyncio
import os
import sys

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent


# ------------------------------------------------------------
# LLM factory — Ollama primary, Groq fallback
# ------------------------------------------------------------


def build_llm():
    """Return a tool-calling chat model.

    Tries Ollama first (free, local, respects the $0 budget).
    Falls back to Groq llama-3.3-70b-versatile when Ollama isn't available
    or the local model can't handle tool-calling reliably.
    """
    try:
        from langchain_ollama import ChatOllama

        # Quick probe: try to instantiate with a short timeout.
        llm = ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0,
        )
        # If the user explicitly prefers cloud, skip local.
        if os.getenv("LLM_PROVIDER", "").lower() == "groq":
            raise RuntimeError("forced to groq")
        print("• LLM: Ollama (local)", file=sys.stderr)
        return llm
    except Exception as e:
        print(f"• Ollama unavailable ({e}); falling back to Groq", file=sys.stderr)

    from langchain_groq import ChatGroq

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError(
            "Ollama unavailable and GROQ_API_KEY is not set. "
            "Set one of them to run this example."
        )
    return ChatGroq(
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        temperature=0,
    )


# ------------------------------------------------------------
# MCP wiring
# ------------------------------------------------------------


async def build_agent():
    """Construct a LangGraph ReAct agent whose tools come from the MCP server."""
    # MultiServerMCPClient spawns the server as a subprocess over stdio —
    # same mechanism Claude Desktop uses.
    client = MultiServerMCPClient(
        {
            "fleetdb": {
                "command": "fleetdb-mcp",
                "args": [],
                "transport": "stdio",
                "env": {
                    "DATABASE_URL": os.environ["DATABASE_URL"],
                    "MCP_ACTOR": os.getenv("MCP_ACTOR", "langgraph-agent"),
                },
            }
        }
    )
    tools = await client.get_tools()
    print(f"• Loaded {len(tools)} tools from the MCP server", file=sys.stderr)

    llm = build_llm()
    agent = create_react_agent(
        llm,
        tools,
        prompt=(
            "You are a fleet operations analyst. You have MCP tools that let you "
            "inspect and query a PostgreSQL fleet database. "
            "For questions, use query_read_only. For changes, use propose_write first, "
            "show the user the preview, and only then call confirm_write. "
            "Cite the SQL you ran."
        ),
    )
    return agent


# ------------------------------------------------------------
# Driver
# ------------------------------------------------------------


async def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python langgraph_client.py "<your question>"', file=sys.stderr)
        sys.exit(1)
    question = " ".join(sys.argv[1:])

    agent = await build_agent()
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})

    # Pretty-print the final assistant message.
    final = result["messages"][-1]
    print("\n" + "=" * 72)
    print(final.content)
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
