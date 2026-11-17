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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, config, present
from . import brand as brands
from .mcp_client import GrapheneMCP, GrapheneUnavailable

log = logging.getLogger("ui_app")
if not log.handlers:  # app messages (Graphene connect/retry) reach stderr -> the systemd journal
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
STATIC = config.ROOT / "app" / "static" / "index.html"
BRAND_ASSETS = config.ROOT / "static" / "brand"  # operator drops logo files here
STATUS_REFRESH_S = 300.0

# Progress lines the skins show while a question runs: SAP terms, no tool or product names.
PROGRESS = {
    "find_object": "Looking up {name}",
    "impact": "Tracing what depends on {name}",
    "assess_transport": "Assessing transport {transport}",
    "status": "Checking what I've learned",
    "operations": "Reviewing jobs, dumps and alerts",
    "system_profile": "Reading the system profile",
}


def progress_text(tool: str, args: dict[str, Any]) -> str:
    t = PROGRESS.get(tool, "Checking what I know")
    try:
        return t.format(name=args.get("name", "the object"), transport=args.get("transport", ""))
    except (KeyError, IndexError):
        return "Checking what I know"


def render_page(b: brands.Brand) -> str:
    """One template for every skin; brand values are injected (the template names no product)."""
    html = STATIC.read_text()
    vars_css = []
    for mode in ("light", "dark"):
        decl = "".join(f"--{k.replace('_', '-')}:{v};" for k, v in b.colors[mode].items())
        vars_css.append(decl)
    return (html.replace("{{TITLE}}", _esc(b.name))
                .replace("{{BRAND_JSON}}", json.dumps(b.public(), ensure_ascii=False).replace("<", "\\u003c"))
                .replace("{{LIGHT_VARS}}", vars_css[0]).replace("{{DARK_VARS}}", vars_css[1]))


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    brand: str = Field(default="partner", max_length=40)


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
            if time.time() - state.get("status_at", 0) < STATUS_REFRESH_S:
                await asyncio.sleep(2.0)  # ready; re-check after a respawn or to refresh the sidebar
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
                if r.ok:
                    state["status"], state["status_at"] = r.structured, time.time()
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

    for b in brands.load().values():
        def page(b: brands.Brand = b) -> HTMLResponse:
            return HTMLResponse(render_page(b), headers={"Cache-Control": "no-store"})
        app.add_api_route(b.path, page, methods=["GET"], response_class=HTMLResponse, name=f"skin_{b.key}")

    @app.get("/brand/{filename}")
    async def brand_asset(filename: str) -> FileResponse:
        allowed = {b.logo_image for b in brands.load().values() if b.logo_image}
        path = BRAND_ASSETS / filename
        if filename not in allowed or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/api/learned")
    async def learned() -> JSONResponse:
        """The skins' "What I've learned" sidebar and status dot, from the engine's status result."""
        ov = present.learned_overview(state.get("status"))
        return JSONResponse({"connected": bool(state["ready"]), **ov}, headers={"Cache-Control": "no-store"})

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
        b = brands.get(body.brand)
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict[str, Any]) -> None:
            # The browser gets neutral progress only; tool names, arguments and results stay inside.
            # Answer text is not streamed: it is shown only after the silent safety check.
            if ev.get("type") == "tool_start":
                await queue.put({"type": "progress", "text": progress_text(ev["tool"], ev.get("arguments") or {})})

        async def run() -> None:
            t0 = time.time()
            rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "brand": b.key, "question": question}
            try:
                if state.get("claude") is None:
                    rec.update(ok=False, error="no Claude API key in .env")
                    await queue.put({"type": "error", "message": brands.ERROR_ANSWER})
                    return
                if not state["ready"]:
                    rec.update(ok=False, error=f"engine not ready: {state['not_ready_reason']}")
                    await queue.put({"type": "error", "message": brands.NOT_READY_ANSWER})
                    return
                out = await agent.answer_with_timeout(question, state["graphene"], state["claude"], emit, b)
                shown = present.view(out, b)
                app.state.last_answer = out  # internal contract (tests, operator debugging); never sent
                rec.update(ok=True, tool_calls=[{k: t[k] for k in ("tool", "arguments", "ok", "ms")}
                                                for t in out["tool_trace"]],
                           grounding_flags=len(out["grounding"]["flags"]), presentation=out["presentation"],
                           usage=out["usage"], timing_ms=out["timing_ms"])
                await queue.put({"type": "final", "data": shown})
            except (agent.AnswerError, GrapheneUnavailable) as e:  # details in the log, never in a skin
                rec.update(ok=False, error=str(e))
                await queue.put({"type": "error", "message": brands.ERROR_ANSWER})
            except Exception as e:  # never leak internals (or anything secret) to the browser
                log.exception("unexpected error")
                rec.update(ok=False, error=type(e).__name__)
                await queue.put({"type": "error", "message": brands.ERROR_ANSWER})
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
