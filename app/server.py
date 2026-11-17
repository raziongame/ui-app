"""FastAPI app on 127.0.0.1:8080. Starts ONE Graphene MCP session at startup and
keeps it for every question. The API key never leaves this process."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, config
from .mcp_client import GrapheneMCP, GrapheneUnavailable

log = logging.getLogger("ui_app")
if not log.handlers:  # app messages (Graphene connect/retry) reach stderr -> the systemd journal
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
STATIC = config.ROOT / "app" / "static" / "index.html"


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def write_log(record: dict[str, Any]) -> None:
    """Questions, tool calls, timings and token usage only. Never the key."""
    config.LOG_PATH.parent.mkdir(exist_ok=True)
    with config.LOG_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def backends_ready(structured: dict[str, Any] | None) -> tuple[bool, str]:
    """Is Graphene ready to answer? Judged only from Graphene's own status result
    (result.health.neo4j / .postgres); the app never connects to those databases itself."""
    health = ((structured or {}).get("result") or {}).get("health") or {}
    down = [name for name in ("neo4j", "postgres") if not (health.get(name) or {}).get("ok")]
    if down:
        return False, "Graphene reports " + " and ".join(down) + " not up yet"
    return True, ""


async def watch_readiness(g: GrapheneMCP, state: dict[str, Any], first_delay: float = 1.0,
                          max_delay: float = 30.0) -> None:
    """Until Graphene's status says Neo4j and Postgres are up, retry with backoff (1, 2, 4 … 30 s).
    Checked again after every graphene-mcp respawn."""
    delay, checked_gen = first_delay, -1
    while True:
        if g.connected and g.generation == checked_gen and state["ready"]:
            await asyncio.sleep(2.0)  # ready; re-check only after a respawn
            continue
        if not g.connected and state["ready"]:
            # A respawn in progress after being ready: calls wait for the new session themselves;
            # the new session (generation change) is checked again below.
            await asyncio.sleep(0.5)
            continue
        if not g.connected:
            ok, why = False, f"graphene-mcp not connected yet ({g.last_error or 'starting'})"
        else:
            gen = g.generation
            try:
                r = await g.call("status", {})
                ok, why = backends_ready(r.structured) if r.ok else (False, f"status failed: {r.error}")
            except GrapheneUnavailable as e:
                ok, why = False, str(e)
            checked_gen = gen
        was = state["ready"]
        state["ready"], state["not_ready_reason"] = ok, (None if ok else why)
        if ok:
            if not was:
                log.info("Graphene ready (Neo4j and Postgres up per Graphene status)")
            delay = first_delay
            continue
        state["retries"] = state.get("retries", 0) + 1
        log.warning("Graphene not ready: %s; retrying in %.0f s", why, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


def create_app(claude_client: Any | None = None, mcp_command: str = config.GRAPHENE_MCP_COMMAND) -> FastAPI:
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        g = GrapheneMCP(mcp_command, call_timeout_s=config.TOOL_TIMEOUT_S)
        # Spawned once, here, without blocking startup (the port opens at once); respawned with
        # backoff if it dies. Questions wait until Graphene's status says Neo4j and Postgres are up.
        await g.start(wait=False)
        state.update(graphene=g, ready=False, not_ready_reason="starting", retries=0)
        watcher = asyncio.create_task(watch_readiness(g, state), name="graphene-readiness")
        app.state.graphene = g  # the one live session (tests and health read it)
        if claude_client is not None:
            state["claude"] = claude_client
        else:
            key = config.load_api_key()
            state["claude"] = (anthropic.AsyncAnthropic(api_key=key, timeout=config.CLAUDE_REQUEST_TIMEOUT_S,
                                                        max_retries=2) if key else None)
            del key
        yield
        watcher.cancel()
        await g.close()

    app = FastAPI(title=config.APP_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(STATIC.read_text(), headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    async def health() -> JSONResponse:
        g: GrapheneMCP = state["graphene"]
        return JSONResponse({
            "app": config.APP_TITLE,
            "model": config.MODEL,
            "claude_key_configured": state.get("claude") is not None,
            "graphene": {
                "ready": state["ready"],
                "not_ready_reason": state["not_ready_reason"],
                "readiness_retries": state["retries"],
                "connected": g.connected,
                "server": g.server_name,
                "version": g.server_version,
                "tools": [t.name for t in g.tools],
                "spawn_count": g.spawn_count,
                "pid": g.pid,
                "connected_since": g.connected_at,
                "last_error": g.last_error,
            },
        })

    @app.post("/api/ask")
    async def ask(body: Ask) -> StreamingResponse:
        question = body.question.strip()
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict[str, Any]) -> None:
            await queue.put(ev)

        async def run() -> None:
            t0 = time.time()
            rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "question": question}
            try:
                if state.get("claude") is None:
                    raise agent.AnswerError("No Claude API key is configured in .env.")
                if not state["ready"]:
                    raise agent.AnswerError(f"Graphene isn't ready yet ({state['not_ready_reason']}). The app "
                                            "keeps retrying; please ask again in a moment.")
                out = await agent.answer_with_timeout(question, state["graphene"], state["claude"], emit)
                rec.update(ok=True, tool_calls=[{k: t[k] for k in ("tool", "arguments", "ok", "ms")}
                                                for t in out["tool_trace"]],
                           grounding_flags=len(out["grounding"]["flags"]), usage=out["usage"],
                           timing_ms=out["timing_ms"])
                await queue.put({"type": "final", "data": out})
            except (agent.AnswerError, GrapheneUnavailable) as e:
                rec.update(ok=False, error=str(e))
                await queue.put({"type": "error", "message": str(e)})
            except Exception as e:  # never leak internals (or anything secret) to the browser
                log.exception("unexpected error")
                rec.update(ok=False, error=type(e).__name__)
                await queue.put({"type": "error", "message": "Unexpected error; see the app log."})
            finally:
                rec["total_ms"] = int((time.time() - t0) * 1000)
                write_log(rec)
                await queue.put(None)

        async def stream():
            task = asyncio.create_task(run())
            try:
                while (ev := await queue.get()) is not None:
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


app = create_app()
