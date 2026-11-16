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

    # operator 2026-11-16: history steps table, built by the app from Graphene's data
    exp = case.get("expect", {})
    if "history_steps" in exp:
        got = [{k: st[k] for k in ("at", "from", "to", "older_engine", "causes")} for st in out["history"]["steps"]]
        assert got == exp["history_steps"]
        assert out["history"]["nothing_changed_since_last"] == exp["nothing_changed_since_last"]
    else:
        assert out["history"] is None


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
    # one turn asking for more distinct calls than the budget
    many = {"calls": [[{"tool": "find_object", "args": {"name": f"ZCAP_TEST_{i}"}}
                       for i in range(config.MAX_TOOL_CALLS + 2)]],
            "answer": {"answer": "Graphene has completed 6 of 11 stages.", "claims": []}}
    fake_claude.messages.override = many
    try:
        out, _ = ask(client, "cap test")
    finally:
        fake_claude.messages.override = None
    assert out is not None
    assert len(out["tool_trace"]) == config.MAX_TOOL_CALLS
    reqs = fake_claude.messages.requests
    assert [r["tool_choice"]["type"] for r in reqs[-2:]] == ["auto", "none"]  # budget used -> must answer


def test_request_shape_to_claude(fake_claude):
    kw = fake_claude.messages.requests[-1]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["thinking"] == {"type": "adaptive"}  # the answer turn thinks (briefly)
    for r in fake_claude.messages.requests:  # the routing turn (first of each question) doesn't
        assert r["thinking"] == ({"type": "between_tools"} if r["n_messages"] == 1 else {"type": "adaptive"})
    assert kw["output_config"]["effort"] == "low"  # operator 2026-11-16: short thinking, < 10 s
    assert kw["output_config"]["format"]["type"] == "json_schema"
    names = {t["name"] for t in kw["tools"]}
    assert names == {"find_object", "impact", "assess_transport", "status", "operations", "system_profile"}
    assert "sample partner app" in kw["system"]
    # operator 2026-11-16: system facts only from system_profile, never from the prompt
    assert "come only from a system_profile result" in kw["system"]
    assert '(e.g. "SP 0")' in kw["system"] and '"not learned yet"' in kw["system"]  # operator, v14
    assert "lead with Graphene's note" in kw["system"]  # operator, v15 + browser review
    # operator 2026-11-16: steps are an app table from Graphene's data; the model only summarises
    assert "set \"history_for\"" in kw["system"] and "don't say that nothing changed" in kw["system"]
    assert "lead with 2-3 sentences" in kw["system"]  # operator: short impact answers (Q1 < 12 s)
    assert "No what-if scores (lowerers) here" in kw["system"]  # operator: short Q2
    assert "answer from assessment.lowerers" in kw["system"]  # operator: golden Q7
    assert "never show field names or JSON words in the answer" in kw["system"]  # operator: browser review
    assert kw["output_config"]["format"]["schema"]["required"] == ["answer", "claims", "history_for"]


SKIP_CASES = [c for c in GOLDEN if "expect_trace" in c]


@pytest.mark.parametrize("case", SKIP_CASES, ids=[c["id"] for c in SKIP_CASES])
def test_call_rounds_and_repeats(client, fake_claude, case):
    """Repeats are answered from the earlier result, not run (make eval 2026-11-14/16). One round of
    Graphene calls per question; a second only after an ambiguous or not-found-with-similar-names
    result (operator 2026-11-16); then Claude must answer."""
    g = client.app.state.graphene
    real_call = g.call
    if case.get("ambiguous_from"):
        parts = [client.portal.call(real_call, "find_object", {"name": n}) for n in case["ambiguous_from"]]
        merged = {**parts[0].structured, "result": {"query": case["mock"]["calls"][0]["args"]["name"], "found": True,
                  "objects": [o for p in parts for o in p.structured["result"]["objects"]]}}

        async def call(name, args):
            if name == "find_object" and args.get("name") == merged["result"]["query"]:
                r = await real_call("find_object", {"name": case["ambiguous_from"][0]})
                r.structured = merged
                return r
            return await real_call(name, args)
        g.call = call
    for fact in case.get("facts", []):  # the exception's trigger is Graphene's real output
        call = next(c for c in case["mock"]["calls"] if not isinstance(c, list) and c["tool"] == fact["tool"])
        assert dig(client.portal.call(real_call, call["tool"], call["args"]).structured, fact["path"]) == fact["equals"]
    fake_claude.messages.override = case["mock"]
    before = len(fake_claude.messages.requests)
    try:
        out, events = ask(client, case["question"])
    finally:
        fake_claude.messages.override = None
        g.call = real_call
    assert out is not None, [e for e in events if e["type"] == "error"]
    assert [t["tool"] for t in out["tool_trace"]] == case["expect_trace"]
    assert out["usage"]["repeat_calls_skipped"] == case["expect_skipped"]
    reqs = fake_claude.messages.requests
    # the calls end (round limit or nothing new) and the last request must answer
    assert reqs[-1]["tool_choice"] == {"type": "none"}
    # exactly the allowed rounds ran, then one answer request; later scripted calls never happen
    mine = reqs[before:]
    assert [r["tool_choice"]["type"] for r in mine] == ["auto"] * case["expect_rounds"] + ["none"]
    skipped = [b for m in reqs[-1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if str(b.get("content", "")).startswith("Not run:")]
    assert len(skipped) == case["expect_skipped"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_answer_text_is_streamed(client, fake_claude, case):
    fake_claude.messages.override = None
    out, events = ask(client, case["question"])
    deltas = [e for e in events if e["type"] == "answer_delta"]
    assert len(deltas) > 1  # arrives in pieces, before the final event
    assert events.index(deltas[-1]) < next(i for i, e in enumerate(events) if e["type"] == "final")
    assert "".join(d["text"] for d in deltas) == out["answer"]


def test_second_round_predicate_uses_graphene_fields():
    from app.agent import needs_second_round
    assert needs_second_round({"result": {"found": False, "similar_names": ["TABL:ZS4_IOT_DATA"]}})
    assert not needs_second_round({"result": {"found": False, "similar_names": []}})
    assert needs_second_round({"result": {"found": True, "objects": [{"key": "A:X"}, {"key": "B:X"}]}})
    assert not needs_second_round({"result": {"found": True, "objects": [{"key": "A:X"}]}})
    assert not needs_second_round(None)


@pytest.mark.parametrize("case", [c for c in GOLDEN if "mock" in c and c["id"] != "grounding_flags_invented_facts"],
                         ids=lambda c: c["id"])
def test_mock_answers_are_plain_language(case):
    """Operator 2026-11-16 (browser review): no field names or JSON words in the answer text; they
    stay in the evidence panel (claims, tool trace)."""
    from app.contract import developer_words
    assert developer_words(case["mock"]["answer"]["answer"]) == []


def test_developer_words_detector():
    from app.contract import developer_words
    assert developer_words("its sp_level is null, score_after 0.395, comparable: false") == \
        ["sp_level", "score_after", "null", "false"]
    assert developer_words("SAP_BASIS 751 · SP 0000 (no support package). TABL:ZS4_IOT_DATA, JOB:JOB-5192e9d4d2ab, "
                           "edge:CLAS:ZCL_X -UNRESOLVED_DYNAMIC-> FUNC:Y") == []

