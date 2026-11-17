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
        h = c.get("/api/health").json()["engine"]
        assert time.time() - t0 < 5  # startup does not block on Graphene
        assert h["ready"] is False and h["not_ready_reason"]
        assert c.get("/").status_code == 200
        out, events = ask(c, "What does system monitoring show?")
        assert out is None
        from app.brand import NOT_READY_ANSWER
        assert [e for e in events if e["type"] == "error"][0]["message"] == NOT_READY_ANSWER


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


def test_operator_surfaces_never_name_the_product(tmp_path):
    """Operator 2026-11-17: /api/health, the journal (log messages) and the unit file are neutral;
    errors that carry the engine's path are neutralised too."""
    import re as _re
    from app.mcp_client import neutral
    from app import config
    from app.server import create_app
    assert neutral("FileNotFoundError: /home/grpadm/graphene/.venv/bin/graphene-mcp") == "FileNotFoundError: engine"
    assert neutral("Graphene MCP down") == "engine MCP down"
    config.LOG_PATH = tmp_path / "app.jsonl"
    missing = "/nonexistent/graphene/.venv/bin/graphene-mcp"
    with TestClient(create_app(claude_client=object(), mcp_command=missing)) as c:
        time.sleep(1.5)
        h = c.get("/api/health").text
        assert "graphene" not in h.lower(), h
    for src in ("app/server.py", "app/mcp_client.py"):
        text = (ROOT / src).read_text()
        for m in _re.finditer(r"log\.(?:info|warning|error|exception)\((.*)", text):
            assert "raphene" not in m.group(1), (src, m.group(0))
    unit = (ROOT / "deploy" / "sap-assistant.service").read_text()
    assert "graphene" not in "\n".join(l for l in unit.splitlines() if not l.startswith("ExecStart")).lower()


def test_eval_runner_stops_at_the_cost_cap(monkeypatch, capsys):
    """Operator 2026-11-17: while fixing, a run is capped at $1; the runner stops before passing it."""
    import asyncio as _asyncio
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "eval"))
    import run_eval

    calls = []

    async def fake_answer(question, g, client, emit, b):
        calls.append(question)
        return {"answer": "x", "claims": [], "tool_trace": [], "grounding": {"ok": True, "flags": []},
                "coverage": [], "freshness": {}, "limits": [], "history": None,
                "usage": {"input_tokens": 0, "output_tokens": 40_000, "requests": 1}, "timing_ms": {}}

    monkeypatch.setattr(run_eval.agent, "answer_with_timeout", fake_answer)  # $0.40 per question
    monkeypatch.setattr(run_eval, "MAX_COST", [1.00])
    monkeypatch.setattr(run_eval, "SKINS", ["partner"])
    monkeypatch.setattr(run_eval, "ONLY", {"impact_zs4_iot_data", "risk_zuzk900008", "risk_zuzk900006",
                                           "not_found_zfake_table"})
    monkeypatch.setattr(run_eval, "RESULTS_DIR", ROOT / "eval" / "results" / "_capcheck")  # kept out of results
    try:
        rc = _asyncio.run(run_eval.run(None, None))
    finally:
        import shutil
        shutil.rmtree(ROOT / "eval" / "results" / "_capcheck", ignore_errors=True)
    assert len(calls) == 2 and rc == 1  # $0.80 spent; a third ($0.40 worst case) would pass $1
    assert "STOPPED: cost cap $1.00" in capsys.readouterr().out
