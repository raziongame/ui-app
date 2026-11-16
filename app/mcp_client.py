"""The app's one and only way into Graphene: its MCP server over stdio.

One long-lived session per app process. A supervisor task spawns
`graphene-mcp` once at startup, keeps the session open for every question,
and respawns it only if the process dies. Nothing here imports Graphene code
or touches its files, databases or SAP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

log = logging.getLogger("ui_app.mcp")


class GrapheneUnavailable(RuntimeError):
    """The MCP session could not be (re)established in time."""


@dataclass
class ToolCallResult:
    ok: bool
    ms: int
    structured: dict[str, Any] | None = None
    error: str | None = None
    reconnected: bool = False


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class _State:
    session: ClientSession | None = None
    generation: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)


def _child_pids(cmd_fragment: str) -> list[int]:
    """PIDs of our own child processes whose command line contains cmd_fragment (Linux /proc)."""
    me = os.getpid()
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                if cmd_fragment.encode() in f.read():
                    out.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return out


class GrapheneMCP:
    def __init__(self, command: str, call_timeout_s: float = 90.0, connect_timeout_s: float = 60.0):
        self.command = command
        self.call_timeout_s = call_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.spawn_count = 0
        self.pid: int | None = None
        self.connected_at: float | None = None
        self.server_name: str | None = None
        self.server_version: str | None = None
        self.instructions: str = ""
        self.tools: list[ToolSpec] = []
        self.last_error: str | None = None
        self._st = _State()
        self._lock = asyncio.Lock()
        self._closing = False
        self._supervisor: asyncio.Task | None = None
        self._restart = asyncio.Event()

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._supervisor = asyncio.create_task(self._supervise(), name="graphene-mcp-supervisor")
        await self._wait_ready()

    async def close(self) -> None:
        self._closing = True
        self._restart.set()
        if self._supervisor:
            try:
                await asyncio.wait_for(self._supervisor, timeout=15)
            except (asyncio.TimeoutError, Exception):
                self._supervisor.cancel()

    @property
    def connected(self) -> bool:
        return self._st.session is not None

    async def _supervise(self) -> None:
        backoff = 1.0
        params = StdioServerParameters(command=self.command)
        while not self._closing:
            try:
                with open(os.devnull, "w") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        self.spawn_count += 1
                        pids = _child_pids("graphene-mcp")
                        self.pid = pids[-1] if pids else None
                        async with ClientSession(read, write) as session:
                            init = await session.initialize()
                            listed = await session.list_tools()
                            self.server_name = init.server_info.name
                            self.server_version = init.server_info.version
                            self.instructions = init.instructions or ""
                            self.tools = [
                                ToolSpec(t.name, (t.description or "").strip(), dict(t.input_schema))
                                for t in listed.tools
                            ]
                            self._st.session = session
                            self._st.generation += 1
                            self.connected_at = time.time()
                            self.last_error = None
                            self._st.ready.set()
                            log.info("graphene-mcp connected (spawn #%d, pid %s, %d tools)",
                                     self.spawn_count, self.pid, len(self.tools))
                            backoff = 1.0
                            await self._hold()  # returns when stopping, asked to restart, or the process died
                            if not self._closing:
                                self.last_error = self.last_error or "graphene-mcp process exited"
                                log.warning("graphene-mcp is gone (%s); respawning", self.last_error)
            except Exception as e:  # process died, pipe closed, init failed
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                log.warning("graphene-mcp session ended: %s", self.last_error)
            finally:
                self._st.session = None
                self._st.ready = asyncio.Event()
                self.pid = None
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    def _alive(self) -> bool:
        """Is the spawned graphene-mcp process still running (not exited, not a zombie)?"""
        if self.pid is None:
            return True  # unknown: rely on call errors instead
        try:
            with open(f"/proc/{self.pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except OSError:
            return False

    async def _hold(self) -> None:
        self._restart = asyncio.Event()
        while not self._closing and not self._restart.is_set() and self._alive():
            try:
                await asyncio.wait_for(self._restart.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _wait_ready(self) -> ClientSession:
        if self._st.session is not None:
            return self._st.session
        try:
            await asyncio.wait_for(self._st.ready.wait(), timeout=self.connect_timeout_s)
        except asyncio.TimeoutError as e:
            raise GrapheneUnavailable(f"Graphene MCP not available: {self.last_error or 'timeout'}") from e
        assert self._st.session is not None
        return self._st.session

    # ---- calls -----------------------------------------------------------
    async def call(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Call one Graphene tool. Retries once only if the session died mid-call."""
        async with self._lock:
            reconnected = False
            for attempt in (1, 2):
                session = await self._wait_ready()
                gen = self._st.generation
                t0 = time.perf_counter()
                try:
                    res = await asyncio.wait_for(
                        session.call_tool(name, arguments, read_timeout_seconds=self.call_timeout_s),
                        timeout=self.call_timeout_s + 5,
                    )
                except asyncio.TimeoutError:
                    ms = int((time.perf_counter() - t0) * 1000)
                    return ToolCallResult(False, ms, error=f"Graphene tool '{name}' timed out after {ms} ms")
                except Exception as e:
                    ms = int((time.perf_counter() - t0) * 1000)
                    died = (not self._alive() or self._st.session is None or self._st.generation != gen
                            or "connection closed" in str(e).lower())
                    if died:
                        self.last_error = f"{type(e).__name__}: {e}"[:300]
                        self._restart.set()  # make the supervisor drop this session and respawn
                        await asyncio.sleep(0.1)
                    if died and attempt == 1:
                        log.warning("graphene-mcp died during %s; reconnecting and retrying once", name)
                        reconnected = True
                        continue
                    return ToolCallResult(False, ms, error=f"{type(e).__name__}: {e}"[:500], reconnected=reconnected)
                ms = int((time.perf_counter() - t0) * 1000)
                structured = res.structured_content
                if structured is None:  # fall back to the JSON text block
                    import json
                    for c in res.content:
                        if getattr(c, "type", None) == "text":
                            try:
                                structured = json.loads(c.text)
                            except ValueError:
                                structured = {"text": c.text}
                            break
                if res.is_error:
                    msg = structured.get("error") if isinstance(structured, dict) else None
                    return ToolCallResult(False, ms, structured=structured,
                                          error=str(msg or "Graphene returned an error"), reconnected=reconnected)
                return ToolCallResult(True, ms, structured=structured, reconnected=reconnected)
            raise GrapheneUnavailable("unreachable")
