"""Hard boundaries from CLAUDE.md that code can check."""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parent.parent
APP_SRC = [p for p in (ROOT / "app").rglob("*") if p.suffix in (".py", ".html")]


def test_no_graphene_imports_or_backend_access():
    bad = re.compile(r"^\s*(from|import)\s+(graphene|neo4j|psycopg|pyrfc|sqlalchemy)\b", re.M)
    for p in APP_SRC:
        assert not bad.search(p.read_text()), p
    # the only path into ~/graphene is the MCP server binary
    for p in APP_SRC:
        for m in re.finditer(r"/home/grpadm/graphene[^\s\"']*", p.read_text()):
            assert m.group(0) == "/home/grpadm/graphene/.venv/bin/graphene-mcp", (p, m.group(0))


def test_page_is_self_contained():
    html = (ROOT / "app" / "static" / "index.html").read_text()
    assert "<title>SAP Assistant · sample partner app</title>" in html
    assert not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", html)
    assert "@import" not in html and "fetch(\"http" not in html


def test_env_is_private_and_ignored():
    env = ROOT / ".env"
    if env.exists():
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    assert r.returncode == 0
    assert "REPLACE_ME" in (ROOT / ".env.example").read_text()


def test_key_never_reaches_browser_or_log(client):
    key = config.load_api_key()
    if not key:
        return
    texts = [client.get("/").text, client.get("/api/health").text]
    if config.LOG_PATH.exists():
        texts.append(config.LOG_PATH.read_text())
    leaked = any(key in t for t in texts)
    assert not leaked, "API key found in page, health response or log"
    assert "***" in repr(config.Secret(key)) and key not in repr(config.Secret(key))


def test_server_binds_localhost_only():
    assert config.HOST == "127.0.0.1" and config.PORT == 8080
    mk = (ROOT / "Makefile").read_text()
    assert "--host 127.0.0.1" in mk and "0.0.0.0" not in mk
