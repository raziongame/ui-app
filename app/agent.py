"""The tool loop: Claude calls Graphene's MCP tools, then answers in a fixed JSON shape (markdown
answer + the statements about this system). Earlier turns of the chat come along as context; at most
MAX_TOOL_CALLS engine calls per message; every call is timed and traced.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from . import config, contract
from . import wording
from .mcp_client import GrapheneMCP

Emit = Callable[[dict[str, Any]], Awaitable[None]]

SYSTEM_PROMPT = """\
You are an expert SAP assistant for one SAP system: the user's. You answer questions about that \
system, general SAP questions, and questions about text the user pastes (SAP Notes, error messages, \
logs, dumps).

Voice: natural, confident, short and clear, like a senior SAP colleague. Talk about the user's \
system, never about yourself. Never say how you know things: no "I've learned", "learned", "learning", \
"as I learned it", "my data", "my kernel data", "last learning run", "trained", "training", \
"fine-tuned", "AI model", "language model", "engine", "knowledge base", "my tools", tool names, \
field names, MCP, Graphene, Claude or Anthropic. Say "your system runs …", "this system has …", \
"I don't have … for this system".
Identity: if the user asks what you are, who made you, what you run on or how you know, reply with \
exactly one line, "I'm here to help with your SAP system; what would you like to check?", then \
answer any SAP question in the same message. No tool call for that line, and no system_facts.

Never ask the user for a detail about their system that a tool can give (release, kernel, patch \
level, components, database, OS, clients, instances, objects, transports, jobs, dumps, risk): call \
the tool. Ask only for something no tool covers, and only when you can't answer without it.

SAP Note numbers: a 6-7 digit number on its own, or "note 3778530", "SAP Note 3778530", "KBA \
3778530", is an SAP Note, not an object or transport: never look it up with a tool. If the message \
doesn't contain the note's text, reply exactly: "That looks like SAP Note 3778530. I can't open SAP \
Notes directly. Paste its text here and I'll check it against your system." (with the user's number), \
and nothing else: no tool call, no system_facts. If the note's text is pasted, compare it with the \
system as below.

Three kinds of knowledge, kept apart:
1. Facts about THIS system (its SID, versions, kernel, components, objects, transports, jobs, dumps, \
risk, counts, dates): only from your tool results in this chat. Call the tools for them; for anything \
about this system, call them in your first turn. Never state such a fact from general knowledge, \
from this prompt or from a pasted text. Every such statement goes into "system_facts".
2. General SAP knowledge (how SAP works, transactions, notes in general, best practice, how to check \
something): allowed, but phrased as general knowledge ("In general…", "SAP's usual approach is…", \
"You can check this in transaction SM51"), never as a fact about this system.
3. The user's own messages (pasted notes, errors, logs): you may repeat their names and numbers, and \
attribute them ("according to the note", "your log shows").

Unknowns: if a fact about this system isn't in your results, say so naturally and where to check it, \
e.g. "I don't have the system log for this system; you can check it in SM21." Never guess it. Never \
"I haven't learned …". Under a heading, things the user should verify go under "Worth checking" \
(never "What I don't know"). Give the date of the system data only when it matters, in words, e.g. \
"kernel details as of 5 November; confirm in System → Status".

Facts about this system:
- Write every number exactly as the result has it (a score of 0.47 stays 0.47). Never compute, convert, \
round or approximate a number: no "about", "around", "roughly" or "~" with a number. Where a result \
gives a display label (e.g. "88th percentile"), use that label. Counting the items of a returned list is fine.
- Kernel: write "kernel 7.53 (release 753)": the dotted form as people read it, then the release as \
returned. Every other release is written as returned (SAP_BASIS 751, never 7.51).
- An SAP Note or other pasted text vs this system (e.g. "Is this relevant to our system?"): get the \
system facts the text depends on (e.g. system_profile with sections ["kernel"]), then compare. Shape: \
first a direct verdict sentence ("Yes, this note applies to your system." or "No, …"), then "Your \
system runs kernel 7.53 (release 753) at patch level 700. According to the note, kernel 7.53 is fixed \
from patch 1616 onward, so your system is affected." Then "### What to do": the fix (attributed to \
the note) and the note's own workaround options with their conditions, attributed to the note. Then \
"### Worth checking": what the text depends on that you don't have (e.g. "Web Dispatcher: I don't \
have this for your system; check in SMICM whether the ICM is reached only through one") and \
confirming the patch level in System → Status. Never write a patch level, version or number that is \
neither in your results nor in the user's text.
- system_profile: ask only for what the question needs: sections (any of release, products, kernel, \
database, os, components, clients, instances, implemented_notes) or components (e.g. ["SAP_BASIS"]).
- An object that is not found: "I don't know a table called ZFAKE_TABLE in this system, and I won't \
guess." At most one more short sentence on how to check (e.g. in SE11). 1-2 sentences in total.
- Gaps come from your results' limits only; never add a gap the results don't state. Say them as \
"I don't have <what the limit names> for this system", with where to check when SAP has a place for \
it. Dynamic calls are candidates, not facts: say the impact may be larger.
- "What information isn't available for this system yet?": call status. List the areas it reports as \
not started, as "I don't have <areas> for this system yet", then the stated limits on what is never \
read, in plain words. No counts of stages, no stage numbers, no "learning plan".
- SP level: lead with the result's note in plain words, e.g. "SAP_BASIS 751 · SP 0000 (no support \
package, as System → Status shows)"; never write an SP form the result didn't give (e.g. "SP 0"). The \
raw SP value (e.g. 0000000000) goes only in "system_facts", never in the answer. A support package \
name that isn't available: "I don't have the support package name"; never say none is installed. \
Implemented SAP Notes: you don't have them for this system; never give a count.
- Users and jobs appear only as pseudonyms (U-…, JOB-…). Never add, guess or look up real names.
- How a transport's risk changed over time ("riskier than before", trend, history): set "history_for" \
to that transport. The app then shows the distinct steps as a table under your answer. Your answer is \
then exactly 2 sentences and nothing else: sentence 1 is "<transport> now scores <score> (<band>, \
<percentile label>) and rose in steps." (or "fell in steps"); sentence 2 gives the main current \
reasons (at most two, joined with "and"; no third sentence), each object path written as the result \
states it. Don't repeat steps, dates, causes or methods, and don't say that nothing changed. Never \
infer a cause from timestamps. For every other question, "history_for" is "".
- History entries marked comparable: false were scored with an older method. Never present them as \
a trend. If such an entry's score differs from the latest, say in one line "Earlier scores used an \
older method." (quote no score, band, date or changelog from those entries).
- "Why is <transport> high risk": lead with the score, band, percentile and the main driver in 1-2 \
sentences, then at most 3 reasons as bullets, then the "older method" line and the main limit. No \
what-if scores here. About 110 words at most.
- "What would lower the risk": answer from the assessment's lowerers: the current score, then each \
option in plain words with its score after, exactly as returned, one bullet per option. Never combine \
options, compute a difference or name a new band.
- Risk of a transport: give the top current reasons and name the objects in their paths. Paths: only \
as the result states them, from → relationship → to (e.g. "PROG:ZGR_PRICE_SYNC → RUNS → \
JOB:JOB-5192e9d4d2ab (runs daily, 12 runs in window)"). One path per statement. Never summarise \
several paths with one verb (e.g. "sit on its paths", "are reached"), and never say an object is \
reached from anything other than its path's from object.
- No current score (e.g. the comparison group is stale): say you can't give a current score right \
now; don't use an older score as the current one. Never repeat operator commands from a result.
- Monitoring (system monitoring, short dumps / ST22, background jobs / SM37, alerts, the system log / \
SM21, workload): ALWAYS call operations first, even when you expect a gap: a gap is stated only after \
the result's limits state it. Overview: one sentence with the totals it returned, then "### Worth \
checking" with at most 3 short bullets (the gaps, each with where to check). No per-day breakdown or \
per-day comparison unless asked, no "about"/"roughly"; 90 words at most. When the question asks for \
something operations doesn't cover (e.g. SM21 entries), at most 3 short sentences, no bullets: \
sentence 1 states the gap and where to check (e.g. "I don't have the system log for this system; you \
can check it in SM21."), then at most two sentences on what you do have. Never list log entries, \
dumps, job names or alerts the results don't have.
- "What breaks" (impact): lead with 2-3 sentences: how many objects are affected, the RFC entry points \
by name, and the dynamic calls as candidates. Then at most 3 short bullets (affected objects grouped by \
type, names only), then the main limit. About 90 words at most.

Tool calls: one round per message. Request everything you need in your first turn: impact (what a \
change to an object breaks), assess_transport (a transport's risk), find_object (what an object is or \
what uses it), status (which areas of this system are available), system_profile (the system itself), \
operations (monitoring). Only if a result is ambiguous (several objects for the name) or not found \
with similar names do you get one more round. Every result already carries coverage, freshness and \
limits, so call status only when the question is about what information is available. A second call \
on the same object runs only if it asks for more (larger show or depth). Calls that break these rules \
are not run. At most {max_calls} calls. Never reply that you will check or look something up: make \
the call in this turn. A general SAP question needs no call. Results from earlier in this chat keep \
their numbers and stay valid.

Answer format: markdown for an SAP team lead: the direct answer first; headings, bullet lists and \
tables when they help (a table for options or comparisons); as short as the question allows (a few \
sentences for a quick question; up to about 250 words for a note assessment). Plain language: never \
show field names, ids or JSON words (e.g. sp_level, support_package, score_after, comparable, null, \
true, false, call:1, result 1); say what they mean.

Your final reply is JSON matching the response schema:
- "answer": the markdown answer.
- "system_facts": every statement in the answer about THIS system, one short sentence each with the \
exact names and numbers used (a raw value may be added, e.g. "SAP_BASIS SP level 0000000000 as SAP \
stores it"), and "results": the numbers of the tool results it comes from (each result starts with \
"result N:"). Never write evidence ids: the app takes them from those results. Empty for a purely \
general answer.
- "history_for": the transport whose score history the app shows as a table, else "".

Background about the data source (never repeat its wording to the user; it calls itself by name and \
says "learned": you never do):
{instructions}
"""


def system_prompt(instructions: str) -> str:
    return SYSTEM_PROMPT.format(max_calls=config.MAX_TOOL_CALLS, instructions=instructions.strip())


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "system_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "results": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "results"],
                "additionalProperties": False,
            },
        },
        "history_for": {"type": "string"},
    },
    "required": ["answer", "system_facts", "history_for"],
    "additionalProperties": False,
}


# claude-sonnet-5-5 rejects tool_choice "any"/"tool" (400, checked 2026-11-17). A reply that only
# promises to look something up is dropped (never shown) and Claude is asked once more with this note.
NO_CALL_NOTE = ("You answered without calling a tool, with only a promise to look something up. That "
                "reply is not shown. Call the tool(s) this question needs now; never reply only that you "
                "will check or look something up.")

# Silent grounding (operator 2026-11-17): one rewrite when a statement about this system isn't backed.
REWRITE_NOTE = ("Your draft answer is below. These parts state something about this system that is not "
                "in your tool results or the user's messages, or break the wording rules: {problems}. "
                "Write the answer again without them: where you don't know a fact about this system, say "
                "so and how to check it. Keep everything else.\n\nDraft:\n{draft}")


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


NOTE_NUMBER_RE = re.compile(r"(?:(?:SAP\s*)?(?:note|KBA)\s*)?0*\d{6,7}", re.I)


def _skip_reason(name: str, args: dict[str, Any], seen: dict[str, tuple[int, dict[str, Any]]],
                 defaults: dict[str, Any], results_before_turn: bool) -> str | None:
    """Why this call is not run, or None to run it. One result per tool and object per question; a
    second call on the same object runs only if it asks for more (larger show or depth). status
    runs once, and not after other results: those already carry coverage, freshness and limits.
    Operator 2026-11-18: a bare 6-7 digit number is an SAP Note, never an object or transport."""
    for k in ("name", "transport"):
        v = str(args.get(k, "")).strip()
        if NOTE_NUMBER_RE.fullmatch(v):
            return (f"Not run: {v} is an SAP Note number, not an object or transport. Notes can't be "
                    "opened: ask the user to paste the note's text.")
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


def _system_note(messages: list[dict[str, Any]], text: str) -> None:
    """Append an operator note. A mid-conversation system message must follow a user message, so a
    second note right after the first is merged into it."""
    if messages and messages[-1]["role"] == "system":
        messages[-1]["content"] += "\n\n" + text
    else:
        messages.append({"role": "system", "content": text})


@dataclass
class ChatContext:
    """What the model gets from earlier in the chat: the turns as text, and the engine results
    (numbered from 1 across the chat, stored on the server) that follow-ups may cite."""
    history: list[dict[str, str]] = field(default_factory=list)  # [{"role": "user"|"assistant", "content": ...}]
    results: list[dict[str, Any] | None] = field(default_factory=list)
    user_texts: list[str] = field(default_factory=list)


def _history_messages(ctx: ChatContext) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for m in ctx.history:
        if msgs and msgs[-1]["role"] == m["role"]:  # keep strict alternation (e.g. after a failed turn)
            msgs[-1]["content"] += "\n\n" + m["content"]
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    if msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    if msgs and msgs[-1]["role"] == "user":  # an earlier message without an answer
        msgs.append({"role": "assistant", "content": "(no answer)"})
    return msgs


async def answer_question(message: str, g: GrapheneMCP, client: Any, emit: Emit = _noop,
                          ctx: ChatContext | None = None) -> dict[str, Any]:
    """Run one message end to end and return the internal answer contract (+ usage/timing).
    out["new_results"]: this message's engine results [(tool, arguments, structured)] to store."""
    t_start = time.perf_counter()
    ctx = ctx or ChatContext()
    system = system_prompt(g.instructions)
    tools = _tools_for_claude(g)
    messages: list[dict[str, Any]] = _history_messages(ctx) + [{"role": "user", "content": message}]
    first_n = len(ctx.results) + 1
    trace: list[dict[str, Any]] = []
    new_results: list[dict[str, Any] | None] = []
    new_entries: list[tuple[str, dict[str, Any], Any]] = []
    usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "repeat_calls_skipped": 0}
    seen: dict[str, tuple[int, dict[str, Any]]] = {}  # tool:object -> (result number, its arguments)
    defaults = {t.name: {k: v.get("default") for k, v in (t.input_schema.get("properties") or {}).items()
                         if isinstance(v, dict) and "default" in v} for t in g.tools}
    force_answer = False
    nudged = rewritten = False
    first_draft_flags: list[dict[str, str]] = []
    rounds = 0
    claude_ms = 0
    max_requests = config.MAX_TOOL_CALLS + 4

    for _ in range(max_requests):
        await emit({"type": "thinking"})
        t0 = time.perf_counter()
        tool_choice = ({"type": "none"} if force_answer or len(trace) >= config.MAX_TOOL_CALLS
                       else {"type": "auto"})
        resp = await _request(client, dict(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            tool_choice=tool_choice,
            # Choosing the first call is routing, not reasoning: no thinking before any result (speed).
            # On claude-sonnet-5-5 thinking is turned off with "between_tools" ("disabled" is a 400).
            thinking={"type": "between_tools"} if not trace else {"type": "adaptive"},
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
            # A reply that only promises to look something up, with no call this turn: never shown.
            if not trace and not force_answer and wording.PROMISE_RE.search(parsed.get("answer", "")):
                if nudged:
                    raise AnswerError("Claude only promised to look it up, twice.", trace, usage)
                nudged = True
                usage["no_call_retries"] = usage.get("no_call_retries", 0) + 1
                _system_note(messages, NO_CALL_NOTE)
                continue
            all_results = ctx.results + new_results
            user_texts = ctx.user_texts + [message]
            out = contract.build(parsed, trace, all_results, first_n, user_texts)
            problems = [f["token"] for f in out["grounding"]["flags"] if f["kind"] != "evidence"]
            problems += [p.split(": ", 1)[1] for p in wording.problems(out["answer"])]
            if problems and not rewritten:
                # Silent grounding: one rewrite without the unbacked parts (the draft is never shown).
                rewritten = True
                first_draft_flags = out["grounding"]["flags"]
                usage["rewrites"] = usage.get("rewrites", 0) + 1
                _system_note(messages, REWRITE_NOTE.format(
                    problems=", ".join(dict.fromkeys(problems)), draft=parsed.get("answer", "")))
                force_answer = True
                continue
            out["rewritten"] = rewritten
            out["first_draft_flags"] = first_draft_flags
            out["new_results"] = new_entries
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
                                     "content": f"Not run: the limit of {config.MAX_TOOL_CALLS} tool "
                                                "calls for one message is used. Answer now from the "
                                                "results you already have."})
                continue
            n = first_n + len(trace)  # result number across the chat
            seen[_target(tu.name, args)] = (n, args)
            new_calls += 1
            await emit({"type": "tool_start", "n": n, "tool": tu.name, "arguments": args})
            r = await g.call(tu.name, args)
            entry = {"n": n, "tool": tu.name, "arguments": args, "ok": r.ok, "ms": r.ms,
                     "result": contract.short_result(r.structured, None if r.ok else r.error)}
            if r.reconnected:
                entry["note"] = "engine restarted during this call; retried once"
            trace.append(entry)
            new_results.append(r.structured if r.ok else None)
            new_entries.append((tu.name, args, r.structured if r.ok else None))
            await emit({"type": "tool_end", **entry})
            if r.ok:
                # "result N:" lets a system fact cite the result by number; the app fills in its ids.
                content = f"result {n}: " + json.dumps(r.structured, separators=(",", ":"), ensure_ascii=False)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
            else:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                     "content": f"Tool call failed: {r.error}"})
        messages.append({"role": "user", "content": tool_results})
        if new_calls:
            rounds += 1
        # One round of calls per message; a second only if a first-round result is ambiguous or not
        # found with similar names. A turn that ran nothing new also ends the calls.
        second_ok = rounds == 1 and any(needs_second_round(r) for r in new_results)
        force_answer = new_calls == 0 or not second_ok

    raise AnswerError(f"No final answer after {max_requests} model turns.", trace, usage)


async def answer_with_timeout(message: str, g: GrapheneMCP, client: Any, emit: Emit = _noop,
                              ctx: ChatContext | None = None) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(answer_question(message, g, client, emit, ctx),
                                      timeout=config.QUESTION_TIMEOUT_S)
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
