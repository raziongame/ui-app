"""FastAPI app on 127.0.0.1:8080 (operator 2026-11-17: Graphene by NiraamAI, chat UI). Starts ONE
engine MCP session at startup and keeps it for every message. Chats are stored per browser (cookie).
The API key never leaves this process; sources and grounding stay in the log and ?debug=1."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, config, present, wording
from .mcp_client import GrapheneMCP, GrapheneUnavailable, neutral
from .store import Store, title_for

log = logging.getLogger("ui_app")
if not log.handlers:  # app messages (engine connect/retry) reach stderr -> the systemd journal
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
STATIC = config.ROOT / "app" / "static" / "index.html"
BRAND_ASSETS = config.ROOT / "static" / "brand"  # the NiraamAI logo (operator)
LOGO_FILES = {"logo.png"}
STATUS_REFRESH_S = 300.0
COOKIE = "gid"
COOKIE_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_MESSAGE_CHARS = 200_000  # long pastes (SAP Notes, logs) are fine

# Progress line while a message runs: SAP terms, no tool names.
PROGRESS = {
    "find_object": "Looking up {name}",
    "impact": "Tracing what depends on {name}",
    "assess_transport": "Assessing transport {transport}",
    "status": "Checking what is available for your system",
    "operations": "Reviewing jobs, dumps and alerts",
    "system_profile": "Reading the system profile",
}


def progress_text(tool: str, args: dict[str, Any]) -> str:
    t = PROGRESS.get(tool, "Checking your system")
    try:
        return t.format(name=args.get("name", "the object"), transport=args.get("transport", ""))
    except (KeyError, IndexError):
        return "Checking your system"


class Ask(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    chat_id: str | None = Field(default=None, max_length=64)


def write_log(record: dict[str, Any]) -> None:
    """Messages (truncated), tool calls, sources, grounding, timings and token usage. Never the key."""
    config.LOG_PATH.parent.mkdir(exist_ok=True)
    with config.LOG_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def browser_id(request: Request) -> tuple[str, bool]:
    """The browser's id from its cookie, or a new one (True = set the cookie)."""
    v = request.cookies.get(COOKIE, "")
    return (v, False) if COOKIE_RE.match(v) else (secrets.token_hex(16), True)


def set_cookie(resp: Response, gid: str) -> None:
    resp.set_cookie(COOKIE, gid, max_age=10 * 365 * 24 * 3600, httponly=True, samesite="lax", path="/")


def model_context(store: Store, chat_id: str) -> agent.ChatContext:
    """Earlier turns as text (failed turns left out), the chat's engine results, the user's texts.
    Each user turn carries the result numbers behind its answer (operator 2026-11-19), so the model sees
    what an earlier answer was based on and never disowns it."""
    history: list[dict[str, Any]] = []
    user_texts: list[str] = []
    for m in store.messages(chat_id, with_meta=True):
        if m["error"]:
            continue
        history.append({"role": m["role"], "content": m["content"]})
        if m["role"] == "user":
            user_texts.append(m["content"])
        else:
            ns = [t["n"] for t in (m.get("meta") or {}).get("tool_trace", []) if isinstance(t.get("n"), int)]
            if ns and len(history) >= 2 and history[-2]["role"] == "user":
                history[-2]["results"] = ns
    return agent.ChatContext(history=history, results=store.results(chat_id), user_texts=user_texts)


def backends_ready(structured: dict[str, Any] | None) -> tuple[bool, str]:
    """Is Graphene ready to answer? Judged only from Graphene's own status result
    (result.health.neo4j / .postgres); the app never connects to those databases itself."""
    health = ((structured or {}).get("result") or {}).get("health") or {}
    down = [name for name in ("neo4j", "postgres") if not (health.get(name) or {}).get("ok")]
    if down:
        return False, "the engine reports " + " and ".join(down) + " not up yet"
    return True, ""


async def watch_readiness(g: GrapheneMCP, state: dict[str, Any], first_delay: float = 1.0,
                          max_delay: float = 30.0) -> None:
    """Until Graphene's status says Neo4j and Postgres are up, retry with backoff (1, 2, 4 … 30 s).
    Checked again after every graphene-mcp respawn."""
    delay, checked_gen = first_delay, -1
    while True:
        if g.connected and g.generation == checked_gen and state["ready"]:
            if time.time() - state.get("status_at", 0) < STATUS_REFRESH_S:
                await asyncio.sleep(2.0)  # ready; re-check after a respawn or every few minutes
                continue
        if not g.connected and state["ready"]:
            # A respawn in progress after being ready: calls wait for the new session themselves;
            # the new session (generation change) is checked again below.
            await asyncio.sleep(0.5)
            continue
        if not g.connected:
            ok, why = False, f"engine not connected yet ({g.last_error or 'starting'})"
        else:
            gen = g.generation
            try:
                r = await g.call("status", {})
                ok, why = backends_ready(r.structured) if r.ok else (False, neutral(f"status failed: {r.error}"))
                if r.ok:
                    state["status"], state["status_at"] = r.structured, time.time()
            except GrapheneUnavailable as e:
                ok, why = False, neutral(str(e))
            checked_gen = gen
        was = state["ready"]
        state["ready"], state["not_ready_reason"] = ok, (None if ok else why)
        if ok:
            if not was:
                log.info("engine ready (Neo4j and Postgres up per its status)")
            delay = first_delay
            continue
        state["retries"] = state.get("retries", 0) + 1
        log.warning("engine not ready: %s; retrying in %.0f s", why, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


def create_app(claude_client: Any | None = None, mcp_command: str = config.GRAPHENE_MCP_COMMAND,
               db_path=None) -> FastAPI:
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        g = GrapheneMCP(mcp_command, call_timeout_s=config.TOOL_TIMEOUT_S)
        # Spawned once, here, without blocking startup (the port opens at once); respawned with
        # backoff if it dies. Messages wait until the engine's status says Neo4j and Postgres are up.
        await g.start(wait=False)
        state.update(graphene=g, ready=False, not_ready_reason="starting", retries=0)
        watcher = asyncio.create_task(watch_readiness(g, state), name="engine-readiness")
        app.state.graphene = g  # the one live session (tests and health read it)
        store = Store(db_path or config.DB_PATH)
        state["store"] = app.state.store = store
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
        store.close()

    app = FastAPI(title=config.APP_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        gid, new = browser_id(request)
        resp = HTMLResponse(STATIC.read_text(), headers={"Cache-Control": "no-store"})
        if new:
            set_cookie(resp, gid)
        return resp

    @app.get("/brand/{filename}")
    async def brand_asset(filename: str) -> FileResponse:
        path = BRAND_ASSETS / filename
        if filename not in LOGO_FILES or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health() -> JSONResponse:
        g: GrapheneMCP = state["graphene"]
        return JSONResponse({
            "app": config.APP_TITLE,
            "model": config.MODEL,
            "claude_key_configured": state.get("claude") is not None,
            "engine": {
                "ready": state["ready"],
                "not_ready_reason": neutral(state["not_ready_reason"]) if state["not_ready_reason"] else None,
                "readiness_retries": state["retries"],
                "connected": g.connected,
                "version": g.server_version,
                "tools": [t.name for t in g.tools],
                "spawn_count": g.spawn_count,
                "pid": g.pid,
                "connected_since": g.connected_at,
                "last_error": neutral(g.last_error) if g.last_error else None,
            },
        })

    @app.get("/api/chats")
    async def list_chats(request: Request) -> JSONResponse:
        gid, new = browser_id(request)
        resp = JSONResponse({"chats": [] if new else state["store"].list_chats(gid)})
        if new:
            set_cookie(resp, gid)
        return resp

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, request: Request, debug: int = 0) -> JSONResponse:
        gid, _ = browser_id(request)
        chat = state["store"].get_chat(gid, chat_id)
        if not chat:
            raise HTTPException(status_code=404)
        msgs = state["store"].messages(chat_id, with_meta=bool(debug))
        return JSONResponse({"chat": chat, "messages": msgs})

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, request: Request) -> JSONResponse:
        gid, _ = browser_id(request)
        if not state["store"].delete_chat(gid, chat_id):
            raise HTTPException(status_code=404)
        return JSONResponse({"deleted": chat_id})

    @app.post("/api/ask")
    async def ask(body: Ask, request: Request, debug: int = 0) -> StreamingResponse:
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=422)
        store: Store = state["store"]
        gid, new_gid = browser_id(request)
        chat = store.get_chat(gid, body.chat_id) if body.chat_id else None
        if body.chat_id and not chat:
            raise HTTPException(status_code=404)
        ctx = model_context(store, chat["id"]) if chat else agent.ChatContext()
        if not chat:
            chat = store.create_chat(gid, title_for(message))
        store.add_message(chat["id"], "user", message)
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict[str, Any]) -> None:
            # The browser gets a neutral progress line only; tool names, arguments and results stay here.
            if ev.get("type") == "tool_start":
                await queue.put({"type": "progress", "text": progress_text(ev["tool"], ev.get("arguments") or {})})

        async def fail(rec: dict[str, Any], text: str, why: str) -> None:
            rec.update(ok=False, error=neutral(why))
            store.add_message(chat["id"], "assistant", text, error=True)
            await queue.put({"type": "error", "message": text})

        async def run() -> None:
            t0 = time.time()
            rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "chat": chat["id"],
                                   "message": message[:2000], "message_chars": len(message)}
            try:
                if state.get("claude") is None:
                    return await fail(rec, wording.ERROR_ANSWER, "no Claude API key in .env")
                if not state["ready"]:
                    return await fail(rec, wording.NOT_READY_ANSWER, f"engine not ready: {state['not_ready_reason']}")
                out = await agent.answer_with_timeout(message, state["graphene"], state["claude"], emit, ctx)
                shown = present.finalize(out)
                meta = present.debug(out)
                app.state.last_answer = out  # internal contract (tests); never sent without ?debug=1
                if out["new_results"]:
                    store.add_results(chat["id"], len(ctx.results) + 1, out["new_results"])
                mid = store.add_message(chat["id"], "assistant", shown, meta=meta)
                rec.update(ok=True, outcome=out["presentation"]["outcome"], system_facts=out["system_facts"],
                           grounding=out["grounding"], presentation=out["presentation"],
                           tool_calls=[{k: t[k] for k in ("n", "tool", "arguments", "ok", "ms")} for t in out["tool_trace"]],
                           usage=out["usage"], timing_ms=out["timing_ms"])
                final = {"type": "final", "answer": shown, "message_id": mid}
                if debug:
                    final["debug"] = meta
                await queue.put(final)
            except (agent.AnswerError, GrapheneUnavailable) as e:  # details in the log only
                await fail(rec, wording.ERROR_ANSWER, str(e))
            except Exception as e:  # never leak internals (or anything secret) to the browser
                log.exception("unexpected error")
                await fail(rec, wording.ERROR_ANSWER, type(e).__name__)
            finally:
                rec["total_ms"] = int((time.time() - t0) * 1000)
                write_log(rec)
                await queue.put(None)

        async def stream():
            yield json.dumps({"type": "chat", "chat": {"id": chat["id"], "title": chat["title"]}}) + "\n"
            task = asyncio.create_task(run())
            try:
                while (ev := await queue.get()) is not None:
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
            finally:
                if not task.done():
                    task.cancel()

        resp = StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        if new_gid:
            set_cookie(resp, gid)
        return resp

    return app


app = create_app()
