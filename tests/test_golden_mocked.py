"""Golden questions end to end: mocked Claude, real Graphene MCP, real chat store.
Operator 2026-11-17: Graphene by NiraamAI, one brand, chat UI, full SAP assistant."""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from conftest import GOLDEN, ask, ask_raw

CASES = [c for c in GOLDEN if not c.get("test_only")]
TOOL_NAMES = ("find_object", "assess_transport", "system_profile", "tool_trace", "evidence_ids", "call:1")


def dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def _run(client, fake_claude, mock, question, chat_id=None, debug=False):
    fake_claude.messages.override = mock
    before = len(fake_claude.messages.requests)
    try:
        out, events, body = ask_raw(client, question, chat_id, debug)
    finally:
        fake_claude.messages.override = None
    return out, events, body, fake_claude.messages.requests[before:]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_contract_and_grounding(client, fake_claude, case):
    fake_claude.messages.override = None
    out, events = ask(client, case["question"])
    assert out is not None, [e for e in events if e["type"] == "error"]
    for k in ("answer", "system_facts", "tool_trace", "coverage", "freshness", "limits", "grounding"):
        assert k in out
    if not case["mock"]["calls"]:  # operator 2026-11-18: identity line, no call, no system facts
        assert out["tool_trace"] == [] and out["system_facts"] == [] and out["grounding"]["ok"]
        assert out["presentation"]["outcome"] == "passed" and out["history"] is None
        return
    # the app fills Graphene's own evidence ids into every system fact (operator 2026-11-17)
    assert out["system_facts"] and all(f["evidence_ids"] for f in out["system_facts"])
    for t in out["tool_trace"]:
        assert set(t) >= {"n", "tool", "arguments", "result", "ms", "ok"} and t["ok"]
    assert [e["type"] for e in events].count("progress") == len(case["mock"]["calls"])
    # the golden answers pass the silent check as written (no rewrite, nothing held back)
    assert out["grounding"]["ok"], out["grounding"]["flags"]
    assert out["presentation"]["outcome"] == "passed", out["presentation"]
    assert out["shown"].startswith(out["answer"])
    from app.wording import problems  # operator 2026-11-18: the app's own table too (no "learned", "engine")
    assert problems(out["shown"]) == [], problems(out["shown"])
    assert out["limits"] and all(isinstance(x, str) for x in out["limits"])
    exp = case.get("expect", {})
    if "history_steps" in exp:  # operator 2026-11-16: the steps table, from Graphene's data
        got = [{k: st[k] for k in ("at", "from", "to", "older_engine", "causes")} for st in out["history"]["steps"]]
        assert got == exp["history_steps"]
        assert "| Date | Score | What changed |" in out["shown"]
        assert out["history"]["nothing_changed_since_last"] == exp["nothing_changed_since_last"]
    else:
        assert out["history"] is None and "| Date | Score |" not in out["shown"]
    for item in exp.get("facts_contain", []):
        assert item in json.dumps(out["system_facts"])


FACT_CASES = [c for c in CASES if c.get("facts")]  # identity cases have no system facts


@pytest.mark.parametrize("case", FACT_CASES, ids=[c["id"] for c in FACT_CASES])
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



@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_browser_gets_only_the_answer(client, fake_claude, case):
    """Operator 2026-11-17: no sources, grounding, timings or tool names in the browser; only the
    chat id, neutral progress lines and the final markdown (debug only with ?debug=1)."""
    fake_claude.messages.override = None
    out, events, body = ask_raw(client, case["question"])
    assert {e["type"] for e in events} <= {"chat", "progress", "final"}
    final = next(e for e in events if e["type"] == "final")
    assert set(final) == {"type", "answer", "message_id"}
    for t in TOOL_NAMES:
        assert t not in body, t
    for e in events:
        if e["type"] == "progress":
            assert not any(t in e["text"] for t in ("impact", "status", "operations", "find_object", "system_profile"))
            assert not re.search(r"learn|\bI\b|engine", e["text"], re.I), e["text"]  # operator 2026-11-18
    assert "trained" not in final["answer"].lower()


def test_debug_view_has_sources_and_grounding(client, fake_claude):
    case = next(c for c in GOLDEN if c["id"] == "note_3778530_relevance")
    out, events, body, _ = _run(client, fake_claude, None, case["question"], debug=True)
    final = next(e for e in events if e["type"] == "final")
    dbg = final["debug"]
    assert dbg["presentation"]["outcome"] == "passed"
    assert dbg["system_facts"][0]["evidence_ids"] == ["node:KERNEL:ZUZ:753:700"]
    assert dbg["tool_trace"][0]["arguments"] == {"sections": ["kernel"]}
    assert dbg["limits"]
    # stored with the chat, readable again with ?debug=1 only
    chat = client.get(f"/api/chats/{out['chat_id']}?debug=1").json()
    assert chat["messages"][1]["meta"]["presentation"]["outcome"] == "passed"
    assert "meta" not in client.get(f"/api/chats/{out['chat_id']}").json()["messages"][1]


def test_unbacked_system_fact_is_rewritten_then_held_back(client, fake_claude):
    """Operator 2026-11-17: the silent check covers statements about this system. Unbacked: one rewrite;
    still unbacked: held back; nothing left: the fallback."""
    from app.wording import FALLBACK_ANSWER, HELD_BACK_NOTE
    for cid in ("grounding_flags_invented_facts", "unbacked_fact_is_rewritten", "held_back_sentence"):
        case = next(c for c in GOLDEN if c["id"] == cid)
        out, events, body, reqs = _run(client, fake_claude, case["mock"], case["question"])
        assert out is not None, (cid, events)
        assert out["presentation"]["outcome"] == case["expect_outcome"], (cid, out["presentation"])
        first = {f["token"] for f in out["first_draft_flags"]}
        for tok in case.get("expect_flags", []):
            assert tok in first, (cid, tok, first)
        assert any("Your draft answer" in m["content"] for m in reqs[-1]["messages_snapshot"] if m["role"] == "system")
        assert reqs[-1]["tool_choice"] == {"type": "none"}
        if cid == "grounding_flags_invented_facts":
            assert out["shown"] == FALLBACK_ANSWER
        if cid == "unbacked_fact_is_rewritten":
            assert "ZS4_IOT_DELETE_DATA" not in out["shown"] and "9 objects" in out["shown"]
        if cid == "held_back_sentence":
            assert "ZS4_IOT_PURGE_DATA" not in out["shown"] and HELD_BACK_NOTE in out["shown"]
            assert "ZS4_IOT_INSERT_DATA" in out["shown"]


def test_general_sap_knowledge_needs_no_call_and_is_not_flagged(client, fake_claude):
    case = next(c for c in GOLDEN if c["id"] == "general_knowledge_needs_no_call")
    out, events, body, reqs = _run(client, fake_claude, case["mock"], case["question"])
    assert out["tool_trace"] == [] and out["presentation"]["outcome"] == "passed"
    assert "7.93" in out["shown"] and len(reqs) == 1


def test_names_and_numbers_from_the_users_text_may_be_repeated(client, fake_claude):
    mock = {"calls": [], "answer": {"answer": "Your log shows ZMY_REPORT failing at line 4711; in general, check ST22.",
                                    "system_facts": [{"text": "Your system runs ZMY_REPORT.", "results": []}], "history_for": ""}}
    out, *_ = _run(client, fake_claude, mock, "Our job ZMY_REPORT dumps at line 4711. Why?")
    assert out["grounding"]["ok"], out["grounding"]["flags"]


def test_kernel_dotted_form_is_backed_by_the_engines_release(client, fake_claude):
    """Operator 2026-11-17: "kernel 7.53 (release 753)" even with no note pasted."""
    mock = {"calls": [{"tool": "system_profile", "args": {"sections": ["kernel"]}}],
            "answer": {"answer": "Your kernel is 7.53 (release 753) at patch 700.",
                       "system_facts": [{"text": "Kernel 7.53 (release 753), patch 700.", "results": [1]}], "history_for": ""}}
    out, *_ = _run(client, fake_claude, mock, "Which kernel do we run?")
    assert out["presentation"]["outcome"] == "passed", out["grounding"]
    bad = {"calls": [{"tool": "system_profile", "args": {"sections": ["kernel"]}}],
           "answer": {"answer": "Your kernel is 7.54 at patch 700.",
                      "system_facts": [{"text": "Kernel 7.54, patch 700.", "results": [1]}], "history_for": ""}}
    out, *_ = _run(client, fake_claude, bad, "Which kernel do we run, again?")
    assert "7.54" in {f["token"] for f in out["first_draft_flags"]}


def test_follow_up_uses_the_chats_earlier_results(client, fake_claude):
    """Earlier turns come along as context; their engine results keep their numbers and back follow-ups."""
    out1, *_ = _run(client, fake_claude, None, "What breaks if I change ZS4_IOT_DATA?")
    cid = out1["chat_id"]
    mock = {"calls": [], "answer": {"answer": "Yes: ZS4_IOT_READ_DATA is one of the 2 RFC entry points.",
                                    "system_facts": [{"text": "ZS4_IOT_READ_DATA is an RFC entry point of ZS4_IOT_DATA.", "results": [1]}],
                                    "history_for": ""}}
    out2, events, body, reqs = _run(client, fake_claude, mock, "Is ZS4_IOT_READ_DATA one of them?", chat_id=cid)
    assert out2["presentation"]["outcome"] == "passed", out2["grounding"]
    assert out2["system_facts"][0]["evidence_ids"] == ["edge:FUNC:ZS4_IOT_READ_DATA-READS_TABLE->TABL:ZS4_IOT_DATA",
                                                       "path:TABL:ZS4_IOT_DATA > FUNC:ZS4_IOT_READ_DATA"]
    roles = [m["role"] for m in reqs[0]["messages_snapshot"]]
    assert roles == ["user", "assistant", "user"]  # the first turn came along as text
    msgs = client.get(f"/api/chats/{cid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # a new call in the follow-up continues the chat's numbering
    mock3 = {"calls": [{"tool": "find_object", "args": {"name": "ZFAKE_TABLE"}}],
             "answer": {"answer": "I don't know a table called ZFAKE_TABLE in this system, and I won't guess.",
                        "system_facts": [{"text": "No object ZFAKE_TABLE in this system.", "results": [2]}], "history_for": ""}}
    out3, *_ = _run(client, fake_claude, mock3, "And ZFAKE_TABLE?", chat_id=cid)
    assert out3["tool_trace"][0]["n"] == 2 and out3["system_facts"][0]["evidence_ids"] == ["call:2"]


def test_tool_call_cap(client, fake_claude):
    from app import config
    many = {"calls": [[{"tool": "find_object", "args": {"name": f"ZCAP_TEST_{i}"}}
                       for i in range(config.MAX_TOOL_CALLS + 2)]],
            "answer": {"answer": "Nothing found for those names.", "system_facts": [], "history_for": ""}}
    out, _, _, reqs = _run(client, fake_claude, many, "cap test")
    assert out is not None
    assert len(out["tool_trace"]) == config.MAX_TOOL_CALLS
    assert [r["tool_choice"]["type"] for r in reqs[-2:]] == ["auto", "none"]  # budget used -> must answer


def test_request_shape_to_claude(client, fake_claude):
    ask(client, "What breaks if I change ZS4_IOT_DATA?")
    kw = fake_claude.messages.requests[-1]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["thinking"] == {"type": "adaptive"}  # the answer turn thinks (briefly)
    for r in fake_claude.messages.requests:  # routing turns (before any result) don't
        assert r["thinking"] == ({"type": "adaptive"} if r["has_results"] else {"type": "between_tools"})
        assert r["tool_choice"]["type"] in ("auto", "none")  # "any" is a 400 on claude-sonnet-5-5
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["schema"]["required"] == ["answer", "system_facts", "history_for"]
    assert kw["output_config"]["format"]["schema"]["properties"]["system_facts"]["items"]["required"] == ["text", "results"]
    assert {t["name"] for t in kw["tools"]} == {"find_object", "impact", "assess_transport", "status", "operations",
                                                "system_profile"}
    text = kw["system"]
    assert "expert SAP assistant for one SAP system" in text and "Cloudbox" not in text and "sample partner app" not in text
    # operator 2026-11-18: never about itself; identity in one line; never ask for system details
    assert "never about yourself" in text and '"I\'ve learned"' in text and '"trained"' in text
    assert "\"I'm here to help with your SAP system; what would you like to check?\"" in text
    assert "Never ask the user for a detail about their system that a tool can give" in text
    # operator 2026-11-18 (answer style): no fixed template or standard headings
    assert "No fixed template and no standard headings" in text and "### " not in text
    assert "Mention a missing fact only when it changes the answer" in text
    assert "you can check it in SM21" in text and "as of 5 November" in text
    assert "General SAP knowledge" in text and "never as a fact about this system" in text
    assert "say so naturally and where to check it" in text  # an unknown system fact
    assert 'write "kernel 7.53 (release 753)"' in text and "SAP_BASIS 751, never 7.51" in text
    assert '"No, this one doesn\'t apply."' in text and '"I can\'t confirm this one yet."' in text
    assert "no CVSS score, priority or symptom" in text and "according to the note" in text
    assert "ask only for what the question needs" in text  # v18 filters
    assert "I don't know a table called ZFAKE_TABLE in this system, and I won't guess." in text
    assert '(e.g. "SP 0")' in text and "goes only in \"system_facts\", never in the answer" in text
    assert "set \"history_for\"" in text and "don't say that nothing changed" in text
    assert "relationship → to" in text and "Never summarise" in text
    assert "lead with 2-3 sentences" in text and "No what-if scores here" in text
    assert "answer from the assessment's lowerers" in text
    assert "never show field names, ids or JSON words" in text
    assert "Never reply that you will check" in text and "A general SAP question needs no call" in text
    assert "call operations" in text and "sentence 1 states the gap" in text
    assert "learning plan" not in text.split("Background about the data source")[0].replace('no "learning plan"', "")
    assert "never add a gap the results don't state" in text
    assert 'Never approximate a number' in text or 'no "about", "around", "roughly"' in text
    assert "Never write evidence ids: the app takes them from those results." in text
    assert "Results from earlier in this chat keep their numbers" in text


def test_reply_that_only_promises_is_not_shown(client, fake_claude):
    """Browser testing 2026-11-17: "I'll check" with no call is dropped; Claude is asked once more."""
    from app.agent import NO_CALL_NOTE
    case = next(c for c in GOLDEN if c["id"] == "no_call_reply_is_retried")
    out, events, body, reqs = _run(client, fake_claude, case["mock"], case["question"])
    assert out is not None, events
    assert [t["tool"] for t in out["tool_trace"]] == ["operations"]
    assert "I'll check" not in out["shown"] and "SM21" in out["shown"]
    assert out["usage"]["no_call_retries"] == 1
    assert reqs[1]["last_message"] == {"role": "system", "content": NO_CALL_NOTE}
    assert reqs[1]["roles"] == ["user", "system"]  # the promise is dropped, not sent back
    assert "I'll check" not in body


def test_second_promise_is_an_error(client, fake_claude):
    from app.wording import ERROR_ANSWER
    case = next(c for c in GOLDEN if c["id"] == "no_call_twice_is_an_error")
    out, events, body, reqs = _run(client, fake_claude, case["mock"], case["question"])
    assert out is None
    errs = [e for e in events if e["type"] == "error"]
    assert errs and errs[-1]["message"] == ERROR_ANSWER and len(reqs) == 2
    cid = next(e["chat"]["id"] for e in events if e["type"] == "chat")
    msgs = client.get(f"/api/chats/{cid}").json()["messages"]
    assert msgs[-1]["error"] is True  # kept in the chat, left out of the model's context later


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


def test_second_round_predicate_uses_graphene_fields():
    from app.agent import needs_second_round
    assert needs_second_round({"result": {"found": False, "similar_names": ["TABL:ZS4_IOT_DATA"]}})
    assert not needs_second_round({"result": {"found": False, "similar_names": []}})
    assert needs_second_round({"result": {"found": True, "objects": [{"key": "A:X"}, {"key": "B:X"}]}})
    assert not needs_second_round({"result": {"found": True, "objects": [{"key": "A:X"}]}})
    assert not needs_second_round(None)

@pytest.mark.parametrize("case", [c for c in GOLDEN if "mock" in c and c["id"] not in
                                  ("grounding_flags_invented_facts", "unbacked_fact_is_rewritten", "held_back_sentence")],
                         ids=lambda c: c["id"])
def test_mock_answers_follow_the_wording_rules(case):
    """Operator 2026-11-18: never about itself (no "learned", "trained", "engine", tool names); no field
    names or JSON words."""
    from app.wording import problems
    from app.contract import developer_words
    answers = case["mock"].get("answers") or [case["mock"]["answer"]]
    for a in answers:
        assert problems(a["answer"]) == [], a["answer"]
        for f in a.get("system_facts", []):
            assert developer_words(f["text"]) == [], f["text"]


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
    # eval 2026-11-17 (4bb6543): correct, the job is reached from the program (was a false positive)
    ok2 = ("The main reasons are the table definition TABL:ZGR_DISC_RULES in the transport and the periodic "
           "job reached from PROG:ZGR_PRICE_SYNC.")
    assert not any(re.search(r, ok2, re.I) for r in rx)
    for bad in ("ZGR_DISC_RULES reaches ZGR_PRICE_SYNC.", "TABL:ZGR_DISC_RULES → READS_TABLE → PROG:ZGR_PRICE_SYNC.",
                "The change to ZGR_DISC_RULES affects ZGR_PRICE_SYNC and the job."):
        assert any(re.search(r, bad, re.I) for r in rx), bad


def test_app_fills_evidence_ids_from_cited_results(client):
    """Operator 2026-11-17: system facts cite results by number; the app fills in Graphene's own ids
    (eval: the model typed FUNC:/FUNGR:ZS4_IOT_FG where Graphene's key is FUGR:ZS4_IOT_FG)."""
    from app.contract import build, fill_evidence
    r = client.portal.call(client.app.state.graphene.call, "impact", {"name": "ZS4_IOT_DATA"}).structured
    fg = fill_evidence({"text": "Function group ZS4_IOT_FG uses table ZS4_IOT_DATA.", "results": [1]}, [r])
    assert fg["evidence_ids"] == ["edge:FUGR:ZS4_IOT_FG-USES_TABLE->TABL:ZS4_IOT_DATA",
                                  "path:TABL:ZS4_IOT_DATA > FUGR:ZS4_IOT_FG"]
    root = fill_evidence({"text": "Table ZS4_IOT_DATA has 9 dependent objects.", "results": [1]}, [r])
    assert root["evidence_ids"] == ["node:TABL:ZS4_IOT_DATA"]
    none = fill_evidence({"text": "No such table in this system.", "results": [1]}, [r])
    assert none["evidence_ids"] == ["call:1"]
    trace = [{"n": 1, "tool": "impact", "arguments": {}, "ok": True, "ms": 1, "result": ""}]
    out = build({"answer": "ok", "system_facts": [{"text": "ZS4_IOT_DATA.", "results": [3]}], "history_for": ""}, trace, [r])
    assert [f["token"] for f in out["grounding"]["flags"]] == ["result 3"]  # not a result of this chat


def test_note_numbers_are_never_looked_up_as_objects(client, fake_claude):
    """Operator 2026-11-18: a 6-7 digit number is an SAP Note; a lookup with it is not run."""
    from app.agent import _skip_reason
    for args in ({"name": "3778530"}, {"transport": "3778530"}, {"name": "note 3778530"}, {"name": "SAP Note 377853"}):
        assert "SAP Note number" in (_skip_reason("find_object", args, {}, {}, False) or ""), args
    for args in ({"name": "ZS4_IOT_DATA"}, {"transport": "ZUZK900008"}, {"name": "12345"}):
        assert _skip_reason("find_object", args, {}, {}, False) is None, args
    mock = {"calls": [{"tool": "find_object", "args": {"name": "3778530"}}],
            "answer": {"answer": "That looks like SAP Note 3778530. I can't open SAP Notes directly. Paste its text "
                                 "here and I'll check it against your system.", "system_facts": [], "history_for": ""}}
    out, _, _, _ = _run(client, fake_claude, mock, "3778530")
    assert out is not None and out["tool_trace"] == [] and out["usage"]["repeat_calls_skipped"] == 1
