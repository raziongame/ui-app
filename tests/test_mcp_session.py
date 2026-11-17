"""The Graphene MCP session: spawned once at startup, reused, respawned only if it dies.
Runs last (zz) because it kills the session."""
from __future__ import annotations

import os
import signal
import time

from conftest import ask


def test_health_reports_one_live_session(client):
    h = client.get("/api/health").json()
    g = h["graphene"]
    assert g["connected"] and g["server"] == "graphene"
    assert g["tools"] == ["find_object", "impact", "assess_transport", "status", "operations",
                          "system_profile"]  # Graphene v14 (1.33.0)
    assert g["spawn_count"] == 1


def test_session_reused_across_questions(client):
    g = client.app.state.graphene
    pid_before, spawns_before = g.pid, g.spawn_count
    for q in ("What uses table ZFAKE_TABLE?", "What haven't you learned yet?"):
        out, _ = ask(client, q)
        assert out is not None
    assert g.pid == pid_before and g.spawn_count == spawns_before == 1


def test_zz_reconnects_only_after_the_process_dies(client):
    g = client.app.state.graphene
    old = g.pid
    assert old
    os.kill(old, signal.SIGKILL)
    deadline = time.time() + 30
    while time.time() < deadline and (g.pid in (None, old)):
        time.sleep(0.2)
    out, _ = ask(client, "What uses table ZFAKE_TABLE?")
    assert out is not None and out["tool_trace"][0]["ok"]
    assert g.spawn_count == 2 and g.pid not in (None, old)


def test_zzz_key_not_in_log_after_all_questions(client):
    from app import config
    key = config.load_api_key()
    if key and config.LOG_PATH.exists():
        leaked = key in config.LOG_PATH.read_text()
        assert not leaked, "API key found in the app log"
    assert config.LOG_PATH.exists() and config.LOG_PATH.read_text().count("\n") >= 5
