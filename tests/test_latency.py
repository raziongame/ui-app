"""G-1 regression: the first assess_transport on a NEW Graphene MCP process answers in < 3 s
(Graphene demo-ready-v10 uses its stored risk baseline instead of rebuilding history)."""
import asyncio

from app.config import GRAPHENE_MCP_COMMAND
from app.mcp_client import GrapheneMCP

FIRST_ASSESS_MAX_MS = 3000


def test_first_assess_on_fresh_session_under_3s():
    async def run():
        g = GrapheneMCP(GRAPHENE_MCP_COMMAND)
        await g.start()
        try:
            r = await g.call("assess_transport", {"transport": "ZUZK900008"})
            return r
        finally:
            await g.close()

    r = asyncio.run(run())
    assert r.ok, r.error
    assert r.ms < FIRST_ASSESS_MAX_MS, f"first assess_transport took {r.ms} ms (see GRAPHENE_ISSUES.md G-1)"
    assert r.structured["result"]["assessment"]["comparison_version"]["source"] == "baseline"
