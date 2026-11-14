"""The tool loop: Claude calls Graphene's MCP tools, then answers in a fixed
JSON shape. One fresh conversation per question; at most MAX_TOOL_CALLS
Graphene calls; every call is timed and traced.
"""

from __future__ import annotations

import asyncio
import json
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
- If the question asks for something Graphene's results do not contain (for example a comparison \
with an earlier score that no result gives), say plainly that Graphene did not return it.
- If the question assumes something the results contradict (e.g. "why is it riskier than before?" \
when Graphene's previous assessment has the same score and lists no changes), say so first, with \
Graphene's numbers and dates, then explain what the results do show. Don't invent a reason for a \
change Graphene doesn't report.
- When a result has a history (e.g. a transport's earlier assessments), show how it changed as \
steps, oldest first. For display only, group consecutive entries whose score, band, engines and \
affected count are all identical into one line with the first and last date (and time if the dates \
are the same day); don't state how many entries a group has. Write every score, count, engine \
version, date and time exactly as Graphene wrote it. Say why a step happened only if Graphene's \
results state the cause; otherwise state what changed between the steps (score, affected count, \
engine version) and say that Graphene does not give the cause.
- Use as few tool calls as answer the question well; you have at most {max_calls}.

Your final reply is JSON matching the response schema:
- "answer": plain language for an SAP team lead, 2-6 short paragraphs or "- " bullet lines. No \
markdown headings, no tables.
- "claims": each factual statement in the answer, with the evidence ids that support it. Use \
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
    pass


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
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    claude_ms = 0
    max_requests = config.MAX_TOOL_CALLS + 3

    for _ in range(max_requests):
        await emit({"type": "thinking"})
        t0 = time.perf_counter()
        resp = await client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": config.EFFORT,
                           "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            cache_control={"type": "ephemeral"},
        )
        claude_ms += int((time.perf_counter() - t0) * 1000)
        usage["requests"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] += getattr(resp.usage, k, 0) or 0

        if resp.stop_reason == "refusal":
            raise AnswerError("Claude declined to answer this question.")
        if resp.stop_reason == "max_tokens":
            raise AnswerError("The answer was cut off (output limit reached).")

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            text = next((b.text for b in resp.content if b.type == "text"), "")
            try:
                parsed = json.loads(text)
            except ValueError as e:
                raise AnswerError("Claude's final answer was not valid JSON.") from e
            out = contract.build(parsed, trace, results)
            out["usage"] = usage
            out["timing_ms"] = {"total": int((time.perf_counter() - t_start) * 1000), "claude": claude_ms,
                                "graphene": sum(t["ms"] for t in trace)}
            out["model"] = config.MODEL
            return out

        messages.append({"role": "assistant", "content": resp.content})
        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            if len(trace) >= config.MAX_TOOL_CALLS:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                     "content": f"Not run: the limit of {config.MAX_TOOL_CALLS} Graphene "
                                                "calls for one question is used. Answer now from the "
                                                "results you already have."})
                continue
            n = len(trace) + 1
            args = dict(tu.input or {})
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

    raise AnswerError(f"No final answer after {max_requests} model turns.")


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
