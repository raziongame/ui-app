"""Service behaviour: starts without Graphene, waits (with backoff) for Graphene's own status to
say Neo4j and Postgres are up, and refuses questions until then. Operator 2026-11-17."""
from __future__ import annotations

import asyncio
import configparser
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import ROOT, ask


def test_backends_ready_reads_graphene_status_only():
    from app.server import backends_ready
    up = {"result": {"health": {"neo4j": {"ok": True}, "postgres": {"ok": True}, "sap": {"ok": None}}}}
    assert backends_ready(up) == (True, "")
    ok, why = backends_ready({"result": {"health": {"neo4j": {"ok": False}, "postgres": {"ok": True}}}})
    assert not ok and "neo4j" in why and "postgres" not in why
    assert backends_ready({"result": {}})[0] is False
    assert backends_ready(None)[0] is False


def test_readiness_retries_with_backoff_until_up():
    from app.server import watch_readiness
    results = [None, {"result": {"health": {"neo4j": {"ok": False}, "postgres": {"ok": False}}}},
               {"result": {"health": {"neo4j": {"ok": True}, "postgres": {"ok": True}}}}]
    sleeps: list[float] = []

    class G:
        connected, generation, last_error = True, 1, None

        async def call(self, name, args):
            assert name == "status"
            r = results.pop(0)
            return SimpleNamespace(ok=r is not None, structured=r, error="pipe closed" if r is None else None)

    state = {"ready": False, "not_ready_reason": "starting", "retries": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        if state["ready"]:
            raise asyncio.CancelledError
        await real_sleep(0)

    async def run():
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await watch_readiness(G(), state, first_delay=1.0, max_delay=30.0)
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

    asyncio.run(run())
    assert state["ready"] and state["retries"] == 2
    assert sleeps[:2] == [1.0, 2.0]  # backoff doubles


def test_app_serves_and_refuses_questions_while_graphene_is_down(tmp_path):
    from app import config
    from app.server import create_app
    config.LOG_PATH = tmp_path / "app.jsonl"
    with TestClient(create_app(claude_client=object(), mcp_command=str(tmp_path / "no-graphene-mcp"))) as c:
        t0 = time.time()
        h = c.get("/api/health").json()["graphene"]
        assert time.time() - t0 < 5  # startup does not block on Graphene
        assert h["ready"] is False and h["not_ready_reason"]
        assert c.get("/").status_code == 200
        out, events = ask(c, "What does system monitoring show?")
        assert out is None
        assert "isn't ready yet" in [e for e in events if e["type"] == "error"][0]["message"]


def test_unit_file_localhost_restart_no_secrets():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(ROOT / "deploy" / "sap-assistant.service")
    svc = cp["Service"]
    assert "--host 127.0.0.1 --port 8080" in svc["ExecStart"]
    assert svc["Restart"] == "always"
    assert cp["Unit"]["StartLimitIntervalSec"] == "0"
    assert cp["Install"]["WantedBy"] == "default.target"
    text = (ROOT / "deploy" / "sap-assistant.service").read_text()
    assert "EnvironmentFile" not in text and "sk-ant" not in text
