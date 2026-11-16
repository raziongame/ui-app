"""Settings. The API key is read from .env and never logged, printed or sent to the browser."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GRAPHENE_MCP_COMMAND = "/home/grpadm/graphene/.venv/bin/graphene-mcp"
MODEL = "claude-sonnet-5-5"
EFFORT = "low"  # operator 2026-11-16: keep thinking short (target < 10 s per answer)
MAX_TOOL_CALLS = 8
TOOL_TIMEOUT_S = 90.0
CLAUDE_REQUEST_TIMEOUT_S = 120.0
QUESTION_TIMEOUT_S = 240.0
MAX_TOKENS = 16000
HOST, PORT = "127.0.0.1", 8080
LOG_PATH = ROOT / "logs" / "app.jsonl"
APP_TITLE = "SAP Assistant · sample partner app"


def load_api_key(env_path: Path = ROOT / ".env") -> str | None:
    """Read ANTHROPIC_API_KEY from this repo's .env only. Returns None if missing."""
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return v or None
    return None


class Secret:
    """Wraps the key so repr/str never reveal it."""
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def get(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__
