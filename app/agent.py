"""The tool loop: Claude calls Graphene's MCP tools, then answers in a fixed
JSON shape. One fresh conversation per question; at most MAX_TOOL_CALLS
Graphene calls; every call is timed and traced.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

import anthropic

from . import config, contract
from .mcp_client import GrapheneMCP

Emit = Callable[[dict[str, Any]], Awaitable[None]]

SYSTEM_PROMPT = """\
You are the assistant inside a *sample partner app*: a demo, built by the Graphene team, of how a \
partner's AI application plugs into Graphene. You are not Graphene. You explain, in plain language, \
what Graphene's tools say about one learned SAP system.

How to answer:
- Call Graphene's tools to get the facts. Every object name, transport, number, date and pseudonym \
in your answer must come from a tool result for this question. Never compute, convert, round or \
estimate a number: write it exactly as Graphene wrote it (e.g. a score of 0.47 stays 0.47). Where \
Graphene gives a display label (e.g. "percentile_label": "88th percentile"), use that label. Counting \
the items of a list Graphene returned is fine.
- If an object is not found, say it is "not found in the learned system" and do not guess what it \
might be or what uses it.
- Pass on what Graphene says it does not know: stages not learned yet, stale history, and that \
dynamic calls are candidates, not facts. Use Graphene's limits for this.
- Facts about the SAP system itself (release, product, SID, components and their versions, database, \
kernel) come only from a system_profile result for this question: call system_profile for them. \
Never state such a fact from anywhere else, including this prompt and Graphene's description of \
itself below. If system_profile is not available or doesn't return the fact, say Graphene did not \
return it. Write a release as returned (751, never 7.51). For an SP level, lead with Graphene's note \
in its own words, e.g. "SAP_BASIS 751 · SP 0000 (no support package, as System → Status shows)"; \
never write an SP form Graphene didn't give (e.g. "SP 0"). The raw SP value goes in a claim, not the \
answer; only if Graphene gives no note does the answer show the raw value. A support package name \
Graphene hasn't learned is "not learned yet" (give Graphene's reason in plain words); never say none \
is installed. Implemented SAP Notes: say they are "not learned yet", as Graphene does; never give a count.
- Users and jobs appear only as pseudonyms (U-…, JOB-…). Never add, guess or look up real names.
- How a transport's risk changed over time ("riskier than before", trend, history): set "history_for" \
to that transport. The app then shows Graphene's distinct steps as a table under your answer, straight \
from Graphene's data (date, score from → to, cause, older engine), followed by Graphene's "nothing \
changed since the last assessment" line when that applies. Your answer is then exactly 2 sentences \
and nothing else (this overrides the general answer format and the other risk rules: no bullets, no \
limits line, no "older engine" line): sentence 1 is "<transport> now scores <score> (<band>, \
<percentile label>) and rose in steps." (or "fell in steps"); sentence 2 gives the main current \
reasons, each object path written as Graphene states it. Don't repeat steps, dates, causes or engines, and \
don't say that nothing changed: the table and the line under it do that. Never infer a cause from \
timestamps. For every other question, "history_for" is "".
- Comparability (other risk answers): history entries Graphene marks comparable: false were scored \
by an older engine. Never present them as a trend. If such an entry's score differs from the latest, \
say in one line "earlier entries used an older engine" (quote no score, band, date or changelog from \
those entries).
- "Why is <transport> high risk" answers: lead with the score, band, percentile and the main driver \
in 1-2 sentences, then at most 3 reasons as bullets, then the one "older engine" line (no changelog \
quote) and the main limit. No what-if scores (lowerers) here. About 110 words at most.
- "What would lower the risk" questions: answer from assessment.lowerers. Give the current score, then \
each option with its "text" and its "score_after" exactly as returned, one bullet per option. Each \
score_after is for that option alone: never combine options, compute a difference or name a new band. \
If Graphene returns no lowerers, say so.
- Risk of a transport: always give Graphene's top current reasons (assessment.reasons), and name the \
objects in their paths. For a history question (history_for set), only the main reasons, in sentence 2.
- Paths: describe how an object is reached only as one of Graphene's paths states it, from → \
relationship → to, using the path's from_key, via_rel and key (e.g. "PROG:ZGR_PRICE_SYNC → RUNS → \
JOB:JOB-5192e9d4d2ab (runs daily, 12 runs in window)"). One path per statement. Never summarise \
several paths with one verb (e.g. "sit on its paths", "are reached"), and never say an object is \
reached from anything other than its path's from object. An object changed in the transport is \
"in the transport", not reached.
- If the question asks for something Graphene's results do not contain, say plainly that Graphene \
did not return it. If Graphene returns no score (e.g. its comparison group is stale), say so and \
pass on what Graphene says would fix it; don't use an older score as the current one, and don't \
speculate about what drives a score Graphene didn't return.
- Tool calls: you get one round. Request everything you need in your first turn; most questions \
need exactly one call: impact (what a change to an object breaks), assess_transport (a transport's \
risk and impact), find_object (what an object is or what uses it), status (what Graphene has \
learned), system_profile (the system itself: release, components, versions, database, kernel). After that round you answer. Only if a result is ambiguous (several objects for the name) \
or not found with similar names do you get one more round, e.g. for the similar name. Every result already \
carries coverage, freshness and limits, so call status only when the question is about what Graphene \
has learned; don't look an object up with find_object when impact or assess_transport already \
answers. Each result stays valid for the whole question: a second call on the same object runs only \
if it asks for more (larger show or depth); status runs only in your first turn. Calls that break \
these rules are not run. You have at most {max_calls} calls.
- "What breaks" (impact) answers: lead with 2-3 sentences: how many objects are affected, the RFC \
entry points by name, and the dynamic calls as candidates (the impact may be larger). Then a compact \
list of at most 3 short bullets (affected objects grouped by type, names only), then the main limit. \
No criticality per object, no description of how each object connects: the evidence panel shows the \
full detail. About 90 words at most.
- Be quick and brief: think briefly; the answer is read on a projector.

Your final reply is JSON matching the response schema:
- "answer": plain language for an SAP team lead, at most about 150 words: the direct answer first, \
then the key facts as "- " bullet lines, then the one or two limits that matter most for this answer \
(the app shows all of Graphene's limits next to it). No markdown headings, no tables. Plain language only: \
never show field names or JSON words in the answer (e.g. sp_level, support_package, score_after, \
percentile_label, comparable, same_as_previous, changes_from_previous, null, true, false); say what \
they mean ("score after", "not learned yet", "older engine"). Where Graphene gives a label or note \
for a value, lead with that label; the raw value belongs in a claim (the evidence panel).
- "claims": the 3-6 key facts of the answer, one short sentence each, with the evidence ids that \
support them. Claims are shown in the evidence panel: there, give Graphene's raw value next to its \
label where they differ (e.g. "SAP_BASIS sp_level 0000000000; Graphene's note: SP 0000 …"). Use \
Graphene's own evidence ids (from "evidence_ids" or "evidence" in the results). If Graphene returned \
no evidence id for a fact (e.g. "not found"), cite the tool call instead as "call:N" (N = 1 for the \
first tool call of this question, 2 for the second, ...).
- "history_for": the transport whose score history the app shows as a table (see above), else "".

What Graphene says about itself:
{instructions}
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_ids"],
                "additionalProperties": False,
            },
        },
        "history_for": {"type": "string"},
    },
    "required": ["answer", "claims", "history_for"],
    "additionalProperties": False,
}


class AnswerError(RuntimeError):
    """A question that produced no answer. Carries what happened so far (trace, usage) for logs/eval."""

    def __init__(self, message: str, trace: list | None = None, usage: dict | None = None):
        super().__init__(message)
        self.trace = trace or []
        self.usage = usage or {}


class AnswerTextStream:
    """Pulls the "answer" string out of Claude's JSON reply while it is still being written, so the
    UI can show the text as it arrives. Display only: the final answer is parsed, built and
    grounding-checked from the complete reply."""

    _ESC = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def __init__(self) -> None:
        self.buf = ""
        self.pos: int | None = None  # index just after the opening quote of the answer value
        self.done = False
        self.text = ""

    def feed(self, chunk: str) -> str:
        """Add raw JSON text; return the newly decoded answer characters (may be empty)."""
        self.buf += chunk
        if self.done:
            return ""
        if self.pos is None:
            m = re.search(r'"answer"\s*:\s*"', self.buf)
            if not m:
                return ""
            self.pos = m.end()
        out: list[str] = []
        i, b = self.pos, self.buf
        while i < len(b):
            c = b[i]
            if c == '"':
                self.done = True
                i += 1
                break
            if c != "\\":
                out.append(c)
                i += 1
                continue
            if i + 1 >= len(b):
                break  # escape not complete yet
            e = b[i + 1]
            if e == "u":
                if i + 6 > len(b):
                    break
                cp = int(b[i + 2:i + 6], 16)
                if 0xD800 <= cp < 0xDC00:  # surrogate pair: need the low half too
                    if i + 12 > len(b):
                        break
                    lo = int(b[i + 8:i + 12], 16)
                    out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                    i += 12
                else:
                    out.append(chr(cp))
                    i += 6
            else:
                out.append(self._ESC.get(e, e))
                i += 2
        self.pos = i
        new = "".join(out)
        self.text += new
        return new


async def _request(client: Any, kw: dict[str, Any], emit: Emit) -> Any:
    """One streamed Claude request. Answer text is emitted as it is written; if the turn turns out
    to be a tool turn after some text, the UI is told to drop that text."""
    ats = AnswerTextStream()
    async with client.messages.stream(**kw) as stream:
        async for ev in stream:
            if ev.type == "text":
                new = ats.feed(ev.text)
                if new:
                    await emit({"type": "answer_delta", "text": new})
            elif ev.type == "content_block_start" and getattr(ev.content_block, "type", "") == "tool_use":
                if ats.text:
                    await emit({"type": "answer_reset"})
                    ats = AnswerTextStream()
        return await stream.get_final_message()


def _target(name: str, args: dict[str, Any]) -> str:
    """The object a call is about: tool + name/transport, without a TYPE: prefix (TABL:X == X)."""
    t = str(args.get("name") or args.get("transport") or "").strip().upper()
    return f"{name}:{t.split(':', 1)[1] if ':' in t else t}"


def _skip_reason(name: str, args: dict[str, Any], seen: dict[str, tuple[int, dict[str, Any]]],
                 defaults: dict[str, Any], results_before_turn: bool) -> str | None:
    """Why this call is not run, or None to run it. One result per tool and object per question; a
    second call on the same object runs only if it asks for more (larger show or depth). status
    runs once, and not after other results: those already carry coverage, freshness and limits."""
    if name == "status" and results_before_turn and _target(name, args) not in seen:
        return ("Not run: the results above already carry coverage, freshness and limits; "
                "answer from them.")
    prev = seen.get(_target(name, args))
    if prev is None:
        return None
    n, prev_args = prev
    asks_more = any(isinstance(args.get(k), int) and args[k] > prev_args.get(k, defaults.get(k, 0))
                    for k in ("show", "depth"))
    if asks_more:
        return None
    return f"Not run: call {n} already returned this for the same object; that result is above and still valid."


def needs_second_round(structured: dict[str, Any] | None) -> bool:
    """A first-round result that justifies one more round of calls, by Graphene's own fields:
    ambiguous (several objects for one name) or not found with similar names."""
    res = (structured or {}).get("result")
    if not isinstance(res, dict):
        return False
    if res.get("found") is False and res.get("similar_names"):
        return True
    return isinstance(res.get("objects"), list) and len(res["objects"]) > 1


def _tools_for_claude(g: GrapheneMCP) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in g.tools]


async def _noop(_: dict[str, Any]) -> None:
    return None


async def answer_question(question: str, g: GrapheneMCP, client: Any, emit: Emit = _noop) -> dict[str, Any]:
    """Run one question end to end and return the answer contract (+ usage/timing)."""
    t_start = time.perf_counter()
    system = SYSTEM_PROMPT.format(max_calls=config.MAX_TOOL_CALLS, instructions=g.instructions.strip())
    tools = _tools_for_claude(g)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    trace: list[dict[str, Any]] = []
    results: list[dict[str, Any] | None] = []
    usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "repeat_calls_skipped": 0}
    seen: dict[str, tuple[int, dict[str, Any]]] = {}  # tool:object -> (call number, its arguments)
    defaults = {t.name: {k: v.get("default") for k, v in (t.input_schema.get("properties") or {}).items()
                         if isinstance(v, dict) and "default" in v} for t in g.tools}
    force_answer = False
    rounds = 0  # rounds of Graphene calls that ran
    claude_ms = 0
    max_requests = config.MAX_TOOL_CALLS + 3

    for _ in range(max_requests):
        await emit({"type": "thinking"})
        t0 = time.perf_counter()
        # Once the Graphene call budget is used (or a turn only repeated earlier calls), Claude must
        # answer: no more tool calls.
        tool_choice = ({"type": "none"} if force_answer or len(trace) >= config.MAX_TOOL_CALLS
                       else {"type": "auto"})
        resp = await _request(client, dict(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            tool_choice=tool_choice,
            # Choosing the first call is routing, not reasoning: no thinking on that turn (speed).
            # On claude-sonnet-5-5 thinking is turned off with "between_tools" ("disabled" is a 400).
            thinking={"type": "between_tools"} if usage["requests"] == 0 else {"type": "adaptive"},
            output_config={"effort": config.EFFORT,
                           "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            cache_control={"type": "ephemeral"},
        ), emit)
        claude_ms += int((time.perf_counter() - t0) * 1000)
        usage["requests"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] += getattr(resp.usage, k, 0) or 0

        if resp.stop_reason == "refusal":
            raise AnswerError("Claude declined to answer this question.", trace, usage)
        if resp.stop_reason == "max_tokens":
            raise AnswerError("The answer was cut off (output limit reached).", trace, usage)

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            text = next((b.text for b in resp.content if b.type == "text"), "")
            try:
                parsed = json.loads(text)
            except ValueError as e:
                raise AnswerError("Claude's final answer was not valid JSON.", trace, usage) from e
            out = contract.build(parsed, trace, results)
            out["usage"] = usage
            out["timing_ms"] = {"total": int((time.perf_counter() - t_start) * 1000), "claude": claude_ms,
                                "graphene": sum(t["ms"] for t in trace)}
            out["model"] = config.MODEL
            return out

        messages.append({"role": "assistant", "content": resp.content})
        tool_results: list[dict[str, Any]] = []
        new_calls = 0
        results_before_turn = bool(trace)
        for tu in tool_uses:
            args = dict(tu.input or {})
            why = _skip_reason(tu.name, args, seen, defaults.get(tu.name, {}), results_before_turn)
            if why:
                usage["repeat_calls_skipped"] += 1
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": why})
                continue
            if len(trace) >= config.MAX_TOOL_CALLS:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                     "content": f"Not run: the limit of {config.MAX_TOOL_CALLS} Graphene "
                                                "calls for one question is used. Answer now from the "
                                                "results you already have."})
                continue
            n = len(trace) + 1
            seen[_target(tu.name, args)] = (n, args)
            new_calls += 1
            await emit({"type": "tool_start", "n": n, "tool": tu.name, "arguments": args})
            r = await g.call(tu.name, args)
            entry = {"n": n, "tool": tu.name, "arguments": args, "ok": r.ok, "ms": r.ms,
                     "result": contract.short_result(r.structured, None if r.ok else r.error)}
            if r.reconnected:
                entry["note"] = "Graphene MCP restarted during this call; retried once"
            trace.append(entry)
            results.append(r.structured if r.ok else None)
            await emit({"type": "tool_end", **entry})
            if r.ok:
                content = json.dumps(r.structured, separators=(",", ":"), ensure_ascii=False)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
            else:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                     "content": f"Graphene call failed: {r.error}"})
        messages.append({"role": "user", "content": tool_results})
        if new_calls:
            rounds += 1
        # One round of Graphene calls per question; a second only if a first-round result is ambiguous
        # or not found with similar names. A turn that ran nothing new also ends the calls.
        second_ok = rounds == 1 and any(needs_second_round(r) for r in results)
        force_answer = new_calls == 0 or not second_ok

    raise AnswerError(f"No final answer after {max_requests} model turns.", trace, usage)


async def answer_with_timeout(question: str, g: GrapheneMCP, client: Any, emit: Emit = _noop) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(answer_question(question, g, client, emit), timeout=config.QUESTION_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise AnswerError(f"No answer within {int(config.QUESTION_TIMEOUT_S)} s.") from e
    except anthropic.AuthenticationError as e:
        raise AnswerError("The Claude API rejected the API key (check .env).") from e
    except anthropic.RateLimitError as e:
        raise AnswerError("The Claude API is rate-limiting this key; try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise AnswerError(f"Claude API error {e.status_code}.") from e
    except anthropic.APIConnectionError as e:
        raise AnswerError("Could not reach the Claude API.") from e
