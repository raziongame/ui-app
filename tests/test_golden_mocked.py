"""Golden questions end to end: mocked Claude, real Graphene MCP."""
from __future__ import annotations

from typing import Any

import pytest

from conftest import GOLDEN, ask

CASES = [c for c in GOLDEN if not c.get("test_only")]


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_contract_and_grounding(client, fake_claude, case):
    fake_claude.messages.override = None
    out, events = ask(client, case["question"])
    assert out is not None, [e for e in events if e["type"] == "error"]

    # answer contract
    for k in ("answer", "claims", "tool_trace", "coverage", "freshness", "limits", "grounding"):
        assert k in out
    assert out["claims"] and all("evidence_ids" in c for c in out["claims"])
    for t in out["tool_trace"]:
        assert set(t) >= {"n", "tool", "arguments", "result", "ms", "ok"} and t["ok"]

    # progress was streamed
    assert [e["type"] for e in events].count("tool_start") == len(case["mock"]["calls"])

    # the mocked answer only says what Graphene returned -> no flags
    assert out["grounding"]["ok"], out["grounding"]["flags"]
    assert out["grounding"]["checked_names"] + out["grounding"]["checked_numbers"] > 0

    # Graphene's limits are passed through verbatim and non-empty
    assert out["limits"] and all(isinstance(x, str) for x in out["limits"])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_facts_from_real_graphene(client, case):
    """Expected numbers come from Graphene. If a learn run changes them, this fails: update
    golden_questions.yaml from Graphene's own output."""
    graphene = client.app.state.graphene
    for fact in case["facts"]:
        call = next(c for c in case["mock"]["calls"] if c["tool"] == fact["tool"])
        r = client.portal.call(graphene.call, call["tool"], call.get("args", {}))
        assert r.ok, r.error
        v = dig(r.structured, fact["path"])
        if "equals" in fact:
            assert v == fact["equals"], (fact, v)
        if "length" in fact:
            assert len(v) == fact["length"], (fact, v)
        if "contains" in fact:
            assert fact["contains"] in v, fact




def test_invented_facts_are_flagged_not_hidden(client, fake_claude):
    case = next(c for c in GOLDEN if c["id"] == "grounding_flags_invented_facts")
    fake_claude.messages.override = case["mock"]
    try:
        out, _ = ask(client, case["question"])
    finally:
        fake_claude.messages.override = None
    flagged = {f["token"] for f in out["grounding"]["flags"]}
    assert not out["grounding"]["ok"]
    for tok in case["expect_flags"]:
        assert tok in flagged, (tok, flagged)
    assert "12 objects" in out["answer"]  # flagged, never removed


def test_tool_call_cap(client, fake_claude):
    from app import config
    many = {"calls": [{"tool": "status", "args": {}}] * (config.MAX_TOOL_CALLS + 2),
            "answer": {"answer": "Graphene has completed 6 of 11 stages.", "claims": []}}
    fake_claude.messages.override = many
    try:
        out, _ = ask(client, "cap test")
    finally:
        fake_claude.messages.override = None
    assert out is not None
    assert len(out["tool_trace"]) == config.MAX_TOOL_CALLS
    reqs = fake_claude.messages.requests
    assert reqs[-1]["tool_choice"] == {"type": "none"}  # budget used -> must answer
    assert [r["tool_choice"]["type"] for r in reqs[-config.MAX_TOOL_CALLS - 1:]] == ["auto"] * config.MAX_TOOL_CALLS + ["none"]


def test_request_shape_to_claude(fake_claude):
    kw = fake_claude.messages.requests[-1]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["format"]["type"] == "json_schema"
    names = {t["name"] for t in kw["tools"]}
    assert names == {"find_object", "impact", "assess_transport", "status", "operations"}
    assert "sample partner app" in kw["system"]
