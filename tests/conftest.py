"""make test: mocked Claude, REAL Graphene MCP. One app (one MCP session) for the whole run."""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest
import yaml
from anthropic.types import Message
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = yaml.safe_load((ROOT / "tests" / "golden_questions.yaml").read_text())


class FakeMessages:
    """Scripted stand-in for client.messages: replays a golden case's mock calls, then its answer."""

    def __init__(self, scripts: dict[str, dict[str, Any]]):
        self.scripts = scripts
        self.requests: list[dict[str, Any]] = []
        self.override: dict[str, Any] | None = None  # force a script regardless of question

    async def create(self, **kw: Any) -> Message:
        return self._reply(kw)

    def stream(self, **kw: Any) -> "FakeStream":
        return FakeStream(self._reply(kw))

    def _reply(self, kw: dict[str, Any]) -> Message:
        self.requests.append({**kw, "n_messages": len(kw["messages"]),  # messages grows in place:
                              "has_results": any(m["role"] == "user" and isinstance(m["content"], list)
                                                 for m in kw["messages"]),  # snapshot at request time
                              "last_message": kw["messages"][-1],
                              "roles": [m["role"] for m in kw["messages"]]})
        msgs = kw["messages"]
        script = self.override or self.scripts[msgs[0]["content"]]
        done = sum(1 for m in msgs if m["role"] == "user" and isinstance(m["content"], list))
        base = {"id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant",
                "model": kw["model"], "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 20}}
        calls = script["calls"]
        # "promise_turns": N = the first N turns only promise to look it up, with no tool call (browser
        # testing 2026-11-17: "I'll check"); the app answers each with a system note (see NO_CALL_NOTE).
        notes = sum(1 for m in msgs if m["role"] == "system")
        if done == 0 and notes < script.get("promise_turns", 0):
            return Message.model_validate({**base, "stop_reason": "end_turn", "content": [
                {"type": "text", "text": json.dumps({"answer": "I'll check the system log for you.",
                                                     "claims": [], "history_for": ""})}]})
        if done < len(calls) and kw.get("tool_choice", {}).get("type") != "none":
            turn = calls[done] if isinstance(calls[done], list) else [calls[done]]  # list = one turn
            return Message.model_validate({**base, "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": f"toolu_{done}_{j}", "name": c["tool"], "input": c.get("args", {})}
                for j, c in enumerate(turn)]})
        return Message.model_validate({**base, "stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(script["answer"])}]})


class FakeStream:
    """Stand-in for client.messages.stream(): text events in small chunks, then the final message."""

    def __init__(self, msg: Message):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for block in self.msg.content:
            yield SimpleNamespace(type="content_block_start", content_block=block)
            if block.type == "text":
                for i in range(0, len(block.text), 7):
                    yield SimpleNamespace(type="text", text=block.text[i:i + 7])

    async def get_final_message(self) -> Message:
        return self.msg


class FakeClaude:
    def __init__(self, scripts):
        self.messages = FakeMessages(scripts)


@pytest.fixture(scope="session")
def fake_claude():
    scripts = {}
    for case in GOLDEN:
        if not case.get("test_only"):
            scripts[case["question"]] = case["mock"]
    return FakeClaude(scripts)


@pytest.fixture(scope="session")
def client(fake_claude, tmp_path_factory):
    from app import config
    config.LOG_PATH = tmp_path_factory.mktemp("logs") / "app.jsonl"
    from app.server import create_app
    with TestClient(create_app(claude_client=fake_claude)) as c:
        import time
        deadline = time.time() + 90  # startup no longer blocks on Graphene: wait until it is ready
        while not c.get("/api/health").json()["graphene"]["ready"] and time.time() < deadline:
            time.sleep(0.2)
        yield c


def ask(client: TestClient, question: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    r = client.post("/api/ask", json={"question": question})
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    final = next((e["data"] for e in events if e["type"] == "final"), None)
    return final, events
