"""make test: mocked Claude, REAL Graphene MCP. One app (one MCP session) for the whole run."""
from __future__ import annotations

import json
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
        self.requests.append(kw)
        msgs = kw["messages"]
        script = self.override or self.scripts[msgs[0]["content"]]
        done = sum(1 for m in msgs if m["role"] == "user" and isinstance(m["content"], list))
        base = {"id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant",
                "model": kw["model"], "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 20}}
        calls = script["calls"]
        if done < len(calls) and kw.get("tool_choice", {}).get("type") != "none":
            c = calls[done]
            return Message.model_validate({**base, "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": f"toolu_{done}", "name": c["tool"], "input": c.get("args", {})}]})
        return Message.model_validate({**base, "stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(script["answer"])}]})


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
        yield c


def ask(client: TestClient, question: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    r = client.post("/api/ask", json={"question": question})
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    final = next((e["data"] for e in events if e["type"] == "final"), None)
    return final, events
