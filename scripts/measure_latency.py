"""Measure Graphene MCP tool latency on ONE warm session (the app's own client).

Usage: uv run python scripts/measure_latency.py [rounds]
Round 1 is the cold call on a fresh session; rounds 2+ are warm.
"""
import asyncio, json, statistics, sys, time
sys.path.insert(0, ".")
from app.config import GRAPHENE_MCP_COMMAND
from app.mcp_client import GrapheneMCP

CALLS = [
    ("find_object", {"name": "ZS4_IOT_DATA"}),
    ("impact", {"name": "ZS4_IOT_DATA"}),
    ("assess_transport", {"transport": "ZUZK900008"}),
    ("assess_transport", {"transport": "ZUZK900006"}),
    ("status", {}),
    ("operations", {}),
]

async def main(rounds: int):
    t0 = time.perf_counter()
    g = GrapheneMCP(GRAPHENE_MCP_COMMAND)
    await g.start()
    startup_ms = int((time.perf_counter() - t0) * 1000)
    timings = {f"{n} {json.dumps(a)}": [] for n, a in CALLS}
    for _ in range(rounds):
        for n, a in CALLS:
            r = await g.call(n, a)
            timings[f"{n} {json.dumps(a)}"].append(r.ms if r.ok else -1)
    out = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "startup_ms": startup_ms,
           "spawn_count": g.spawn_count, "server": f"{g.server_name} {g.server_version}", "rounds": rounds,
           "calls": {k: {"cold_ms": v[0], "warm_ms": v[1:], "warm_median_ms": int(statistics.median(v[1:])) if v[1:] else None}
                     for k, v in timings.items()}}
    await g.close()
    print(json.dumps(out, indent=2))

asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4))
