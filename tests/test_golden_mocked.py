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

    # neutral progress was streamed (one line per tool call; no tool names)
    assert [e["type"] for e in events].count("progress") == len(case["mock"]["calls"])
    # operator 2026-11-17: the golden answers pass the silent safety check (shown, not the fallback)
    assert out["view"]["fallback"] is False, out["presentation"]
    assert out["view"]["answer"] == out["answer"]

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
    assert "12 objects" in out["answer"]  # flagged in the internal contract and the log
    # operator 2026-11-17 (silent safety): the skin never shows it; it shows the fallback instead
    from app.brand import FALLBACK_ANSWER
    assert out["view"] == {"answer": FALLBACK_ANSWER, "fallback": True, "sources": [], "learned": [], "history": None}
    assert not out["presentation"]["shown"]


def test_tool_call_cap(client, fake_claude):
    from app import config
    # one turn asking for more distinct calls than the budget
    many = {"calls": [[{"tool": "find_object", "args": {"name": f"ZCAP_TEST_{i}"}}
                       for i in range(config.MAX_TOOL_CALLS + 2)]],
            "answer": {"answer": "I have learned 6 of 11 areas.", "claims": []}}
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
    for r in fake_claude.messages.requests:  # routing turns (before any Graphene result) don't
        assert r["thinking"] == ({"type": "adaptive"} if r["has_results"] else {"type": "between_tools"})
        assert r["tool_choice"]["type"] in ("auto", "none")  # "any" is a 400 on claude-sonnet-5-5
    assert kw["output_config"]["effort"] == "low"  # operator 2026-11-16: short thinking, < 10 s
    assert kw["output_config"]["format"]["type"] == "json_schema"
    names = {t["name"] for t in kw["tools"]}
    assert names == {"find_object", "impact", "assess_transport", "status", "operations", "system_profile"}
    # operator 2026-11-17: white-label persona and voice, per skin
    from app.agent import system_prompt
    from app.brand import get
    partner, direct = system_prompt(get("partner"), "x"), system_prompt(get("direct"), "x")
    assert "You are Cloudbox · SAP Assistant" in partner and 'never write the word "Graphene"' in partner
    assert "You are Graphene by NiraamAI" in direct and 'never write the word "Graphene"' not in direct
    assert any("You are Cloudbox" in r["system"] for r in fake_claude.messages.requests)
    for text in (partner, direct):
        assert "sample partner app" not in text
        assert 'Never say trained' in text and '"I\'ve learned"' in text
        assert "I don't know a table called ZFAKE_TABLE in this system, and I won't guess." in text
        assert "come only from a system_profile result" in text
        assert '(e.g. "SP 0")' in text and "lead with the result's note" in text  # operator, v14/v15
        assert "set \"history_for\"" in text and "don't say that nothing changed" in text
        assert "relationship → to" in text and "Never summarise" in text  # v3 paths
        assert "lead with 2-3 sentences" in text  # operator: short impact answers (Q1 < 12 s)
        assert "No what-if scores here" in text  # operator: short Q2
        assert "answer from the assessment's lowerers" in text  # operator: golden Q7
        assert "never show field names, ids or JSON words" in text  # operator: browser review
        assert "Never reply that you will check" in text  # browser testing 2026-11-17
        assert "call operations" in text and "sentence 1 states the gap" in text
        # "next on my learning plan" only for stages the status lists as not started (facts rule)
        assert "Only call something \"next on my learning plan\" when the status result lists" in text


def _run_override(client, fake_claude, case_id):
    case = next(c for c in GOLDEN if c["id"] == case_id)
    fake_claude.messages.override = case["mock"]
    before = len(fake_claude.messages.requests)
    try:
        out, events = ask(client, case["question"])
    finally:
        fake_claude.messages.override = None
    return out, events, fake_claude.messages.requests[before:]


def test_reply_without_a_graphene_call_is_not_shown(client, fake_claude):
    """Browser testing 2026-11-17: "I'll check" with 0 Graphene calls. The app drops that reply, asks
    once more with a system note, and answers from the operations call."""
    from app.agent import NO_CALL_NOTE
    out, events, reqs = _run_override(client, fake_claude, "no_call_reply_is_retried")
    assert out is not None, [e for e in events if e["type"] == "error"]
    assert [t["tool"] for t in out["tool_trace"]] == ["operations"]
    assert "I'll check" not in out["answer"] and "SM21" in out["answer"]
    assert out["usage"]["no_call_retries"] == 1
    assert reqs[1]["last_message"] == {"role": "system", "content": NO_CALL_NOTE}
    assert reqs[1]["roles"] == ["user", "system"]  # the promise reply is dropped, not sent back
    assert {e["type"] for e in events} <= {"progress", "final"}  # the promise never reached the browser
    assert out["grounding"]["ok"], out["grounding"]["flags"]


def test_second_reply_without_a_call_is_an_error(client, fake_claude):
    out, events, reqs = _run_override(client, fake_claude, "no_call_twice_is_an_error")
    assert out is None
    from app.brand import ERROR_ANSWER
    errs = [e for e in events if e["type"] == "error"]
    assert errs and errs[-1]["message"] == ERROR_ANSWER  # details in the log only
    assert len(reqs) == 2


def test_monitoring_eval_checks_reject_promises_and_log_lines():
    import re
    for cid in ("monitoring_overview", "sm21_details"):
        case = next(c for c in GOLDEN if c["id"] == cid)
        rx = case["expect"]["answer_not_matches"]
        assert not any(re.search(r, case["mock"]["answer"]["answer"], re.I) for r in rx)
        for bad in ("I'll check the system log for you.", "I will look it up.", "Let me check SM21."):
            assert any(re.search(r, bad, re.I) for r in rx), bad
    rx = next(c for c in GOLDEN if c["id"] == "sm21_details")["expect"]["answer_not_matches"]
    assert any(re.search(r, "SM21 at 14:02: dialog process restarted", re.I) for r in rx)


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


@pytest.mark.parametrize("skin", ["partner", "direct"])
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_skin_receives_only_the_checked_view(client, fake_claude, case, skin):
    """Operator 2026-11-17: the browser gets progress lines and the checked answer only. No answer
    text before the safety check, no tool names, ids, field names or JSON internals; the partner skin
    never receives the word Graphene (anywhere in the stream)."""
    import json as _json
    from conftest import ask_raw
    fake_claude.messages.override = None
    out, events, body = ask_raw(client, case["question"], skin)
    assert out is not None and out["view"]["fallback"] is False, out and out["presentation"]
    assert {e["type"] for e in events} <= {"progress", "final"}
    view = out["view"]
    assert set(view) == {"answer", "fallback", "sources", "learned", "history"}
    for tool in ("find_object", "assess_transport", "system_profile", "tool_trace", "evidence_ids", "call:1",
                 "grounding", "coverage"):
        assert tool not in body, tool
    for e in events:
        if e["type"] == "progress":
            assert not any(t in e["text"] for t in ("impact", "status", "operations"))
    if skin == "partner":
        assert "graphene" not in body.lower()
    for text in [view["answer"], *view["sources"]]:
        assert "trained" not in text.lower()
    assert view["sources"], "How do I know? has sources"
    assert all(_json.dumps(l) and l["area"] for l in view["learned"])


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
    from app.brand import get, wording_problems
    from app.contract import developer_words
    assert developer_words(case["mock"]["answer"]["answer"]) == []
    for cl in case["mock"]["answer"]["claims"]:  # claims are the "How do I know?" sources now
        assert developer_words(cl["text"]) == [], cl["text"]
    for skin in ("partner", "direct"):
        texts = [case["mock"]["answer"]["answer"], *(cl["text"] for cl in case["mock"]["answer"]["claims"])]
        assert [p for t in texts for p in wording_problems(t, get(skin))] == []


def test_developer_words_detector():
    from app.contract import developer_words
    assert developer_words("its sp_level is null, score_after 0.395, comparable: false") == \
        ["sp_level", "score_after", "null", "false"]
    assert developer_words("SAP_BASIS 751 · SP 0000 (no support package). TABL:ZS4_IOT_DATA, JOB:JOB-5192e9d4d2ab, "
                           "edge:CLAS:ZCL_X -UNRESOLVED_DYNAMIC-> FUNC:Y") == []


def test_history_dates_display_only():
    """Operator 2026-11-16 (v3): "12 Nov 2026, 12:53 UTC" in the table; formatting only, no conversion."""
    from app.contract import display_utc
    assert display_utc("2026-11-12T12:53:47+00:00") == "12 Nov 2026, 12:53 UTC"
    assert display_utc("2026-11-05T14:26:30.307039+00:00") == "5 Nov 2026, 14:26 UTC"
    assert display_utc("2026-11-12T12:53:47Z") == "12 Nov 2026, 12:53 UTC"
    assert display_utc("2026-11-12T13:53:47+01:00") == "2026-11-12T13:53:47+01:00"  # not UTC: as given
    assert display_utc(None) == ""


def test_q3_path_checks_catch_the_v2_wording():
    """The eval's Q3 path checks reject the partner-demo-v2 answer and accept Graphene's path form."""
    import re
    case = next(c for c in GOLDEN if c["id"] == "risk_zuzk900006")
    rx = case["expect"]["answer_not_matches"]
    v2 = ("The main reason is that the transport contains table definition TABL:ZGR_DISC_RULES, and programs "
          "ZGR_PRICE_SYNC and ZGR_DISC_REPORT sit on its paths.")
    assert any(re.search(r, v2, re.I) for r in rx)
    assert not any(re.search(r, case["mock"]["answer"]["answer"], re.I) for r in rx)
    # eval 2026-11-16 (23f8db8): Graphene's own path form must pass (was a false positive on "the path")
    ok = ("The main current reasons are the table definition TABL:ZGR_DISC_RULES in the transport, and the path "
          "PROG:ZGR_PRICE_SYNC → RUNS → JOB:JOB-5192e9d4d2ab (runs daily, 12 runs in window). A further reason "
          "is the path TABL:ZGR_DISC_RULES → READS_TABLE → PROG:ZGR_DISC_REPORT.")
    assert not any(re.search(r, ok, re.I) for r in rx)
    for bad in ("ZGR_DISC_RULES reaches ZGR_PRICE_SYNC.", "TABL:ZGR_DISC_RULES → READS_TABLE → PROG:ZGR_PRICE_SYNC.",
                "The change to ZGR_DISC_RULES affects ZGR_PRICE_SYNC and the job."):
        assert any(re.search(r, bad, re.I) for r in rx), bad

