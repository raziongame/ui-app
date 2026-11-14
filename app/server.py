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
STATIC = config.ROOT / "app" / "static" / "index.html"


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def write_log(record: dict[str, Any]) -> None:
    """Questions, tool calls, timings and token usage only. Never the key."""
    config.LOG_PATH.parent.mkdir(exist_ok=True)
    with config.LOG_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_app(claude_client: Any | None = None, mcp_command: str = config.GRAPHENE_MCP_COMMAND) -> FastAPI:
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        g = GrapheneMCP(mcp_command, call_timeout_s=config.TOOL_TIMEOUT_S)
        try:
            await g.start()  # spawned once, here; reconnects only if it dies
        except GrapheneUnavailable as e:
            log.error("%s (the app keeps retrying in the background)", e)
        state["graphene"] = g
        app.state.graphene = g  # the one live session (tests and health read it)
        if claude_client is not None:
            state["claude"] = claude_client
        else:
            key = config.load_api_key()
            state["claude"] = (anthropic.AsyncAnthropic(api_key=key, timeout=config.CLAUDE_REQUEST_TIMEOUT_S,
                                                        max_retries=2) if key else None)
            del key
        yield
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
