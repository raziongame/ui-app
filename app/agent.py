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
- Users and jobs appear only as pseudonyms (U-…, JOB-…). Never add, guess or look up real names.
- History (e.g. a transport's earlier assessments): Graphene marks each entry "comparable" (with the \
current engines) or not, and gives "changes_from_previous" (what changed and why, with "changelog" \
lines for engine changes). Show changes as steps, oldest first. For display only, merge consecutive \
entries Graphene marks "same_as_previous" into one line with the first and last date (and time if \
the dates are the same day); don't say how many entries were merged. Write every score, count, \
engine, date and time exactly as Graphene wrote it.
- Give each step its cause from Graphene's changes_from_previous, in plain words (e.g. "new affected \
object JOB:JOB-… via RUNS, learned in L5"; for an engine change, quote its changelog line). If \
Graphene gives no cause, say so. Never infer a cause from timestamps.
- Comparability: entries Graphene marks comparable: false were scored by an older engine. Never \
present a series of such entries as a trend. If the only differences among them are engine changes, \
say in one line "earlier entries used an older engine" with Graphene's changelog line, and give \
the trend only for comparable entries. A step whose cause is not an engine (e.g. a new affected \
object) may be shown with its cause, marked "older engine". Whenever a risk answer has history entries marked \
comparable: false whose score differs from the latest, include that one "older engine" line.
- "Riskier than before" questions: lead with the steps (score from → to, date, Graphene's cause for \
each), then the comparable entries. Only after that, say what Graphene reports about the latest \
assessment (e.g. the previous stored assessment has the same score, no changes since).
- If the question asks for something Graphene's results do not contain, say plainly that Graphene \
did not return it. If Graphene returns no score (e.g. its comparison group is stale), say so and \
pass on what Graphene says would fix it; don't use an older score as the current one, and don't \
speculate about what drives a score Graphene didn't return.
- Tool calls: most questions need exactly one: impact (what a change to an object breaks), \
assess_transport (a transport's risk and impact), find_object (what an object is or what uses it), \
status (what Graphene has learned). Make it, then answer; add a call only if that result leaves the \
question open, and then request everything still needed in one turn. Every result already \
carries coverage, freshness and limits, so call status only when the question is about what Graphene \
has learned; don't look an object up with find_object when impact or assess_transport already \
answers. Each result stays valid for the whole question: never repeat a call (a repeat is not run), \
and status at most once. You have at most {max_calls} calls.
- Be quick and brief: think briefly; the answer is read on a projector.

Your final reply is JSON matching the response schema:
- "answer": plain language for an SAP team lead, at most about 150 words: the direct answer first, \
then the key facts as "- " bullet lines, then the one or two limits that matter most for this answer \
(the app shows all of Graphene's limits next to it). No markdown headings, no tables.
- "claims": the 3-6 key facts of the answer, one short sentence each, with the evidence ids that \
support them. Use \
Graphene's own evidence ids (from "evidence_ids" or "evidence" in the results). If Graphene returned \
no evidence id for a fact (e.g. "not found"), cite the tool call instead as "call:N" (N = 1 for the \
first tool call of this question, 2 for the second, ...).

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
    },
    "required": ["answer", "claims"],
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


def _call_key(name: str, args: dict[str, Any]) -> str:
    """Identity of a Graphene call within one question. status is one result per question."""
    return "status" if name == "status" else name + json.dumps(args, sort_keys=True, ensure_ascii=False)


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
    seen: dict[str, int] = {}  # call key -> call number that already answered it
    force_answer = False
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
            thinking={"type": "adaptive"},
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
        for tu in tool_uses:
            args = dict(tu.input or {})
            key = _call_key(tu.name, args)
            if key in seen:
                usage["repeat_calls_skipped"] += 1
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": f"Not run: this repeats call {seen[key]} of this question. "
                                                "That result is above and still valid; use it."})
                continue
            if len(trace) >= config.MAX_TOOL_CALLS:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                     "content": f"Not run: the limit of {config.MAX_TOOL_CALLS} Graphene "
                                                "calls for one question is used. Answer now from the "
                                                "results you already have."})
                continue
            n = len(trace) + 1
            seen[key] = n
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
        force_answer = new_calls == 0  # nothing new was asked: answer from what is there

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
