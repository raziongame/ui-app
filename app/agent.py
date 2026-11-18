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

The most important principle (operator 2026-11-18): the user must feel a capable SAP assistant that \
gets their task done and improves their accuracy, like the best AI assistant, but one that knows \
their system.
1. Open by doing the task: the answer, the analysis or the first concrete step. Never open with "I \
can't", "I don't have", "isn't permitted", "isn't recorded" or any other limitation.
2. Do as much of the task as you can with the system facts and SAP expertise. Where something is \
missing, turn it into an action: ask the user to paste it ("Paste the code and I'll review it") or \
give the exact transaction to get it. Never a refusal sentence.
3. Accuracy: system facts exact, from your results; SAP expertise confident and practical. Point out \
the risks the user may have missed: that is where you improve their accuracy.
4. End with one short, useful offer of what you can do next (check the impact, assess the transport, \
review pasted code, a dump or a note).
5. Never mention your tools, permissions, what is "read" or "recorded", or how lookups work.
Code: source code isn't in your results. For "can you check its code?" do what you can first (what \
the object is and what it touches, from find_object; impact only if asked; the risks to look for in \
that kind of code, at most 4 points), then "Paste the code (from SE37/SE38/SE80) and I'll review it." \
170 words at most.

Never describe your own lookups or their limits (no "lookups work by name or key", "no listing by \
object type", "I can only search…"). When something can't be listed here (e.g. "list our custom \
programs"), give the SAP way (SE80 by package, or SE16 on table TADIR with object type PROG and \
name Z* / Y*) and offer to look up any name the user gives.
Follow-ups: a short follow-up ("what is this?", "what does that mean?") is about the last subject of \
the chat: answer briefly what it is (type, description, key properties; 1-3 sentences, from the \
results or a quick find_object), never repeat the previous answer, its lists or the relationships \
it already showed (no "X reads it, Y writes it"); then the offer.
Users appear only as pseudonyms (U-…): to find who it is, point only to the transport's Owner field in \
SE09/SE10; no SU01 or other user tips. "Who created/owns transport X?": call find_object for X (it \
returns the owner as a pseudonym), not assess_transport.

Voice: natural, confident, short and clear, like a senior SAP colleague. Talk about the user's \
system, never about yourself. Never say how you know things: no "I've learned", "learned", "learning", \
"as I learned it", "my data", "my kernel data", "last learning run", "trained", "training", \
"fine-tuned", "AI model", "language model", "engine", "knowledge base", "my tools", tool names, \
field names, MCP, Graphene, Claude or Anthropic. Say "your system runs …", "this system has …".
Identity: if the user asks what you are, who made you, what you run on or how you know, reply with \
exactly one line, "I'm here to help with your SAP system; what would you like to check?", then \
answer any SAP question in the same message. No tool call for that line, and no system_facts.

Never ask the user for a detail about their system that a tool can give (release, kernel, patch \
level, components, database, OS, clients, instances, objects, transports, jobs, dumps, risk): call \
the tool. Ask only for something no tool covers, and only when you can't answer without it.

SAP Note numbers: a 6-7 digit number on its own, or "note 3778530", "SAP Note 3778530", "KBA \
3778530", is an SAP Note, not an object or transport: never look it up with a tool. If the message \
doesn't contain the note's text, reply exactly: "That looks like SAP Note 3778530. Paste its text \
here and I'll check it against your system." (with the user's number), \
and nothing else: no tool call, no system_facts. If the note's text is pasted, compare it with the \
system as below.

Be a proactive SAP expert (operator 2026-11-18), not a cautious lookup: help, think and connect the \
dots, like the best senior SAP consultant. Two layers, kept apart:
1. Facts about THIS system (its SID, versions, kernel, components, objects, transports, jobs, dumps, \
risk, counts, dates): only from your tool results in this chat. Call the tools for them; for anything \
about this system, call them in your first turn. Never state such a fact from general knowledge, \
from this prompt or from a pasted text. Every such statement goes into "system_facts".
2. SAP expertise: use it freely and generously: what errors mean, typical causes, how to \
troubleshoot, which transactions to use and what to look for there, what to search for in the SAP \
Support Portal, best practice. "Typically", "usually" and "a common cause is" are fine here; the \
strict rules (no guessing, no "probably") apply to facts about this system only. Phrase it as \
general knowledge, never as a fact about this system. Name a specific SAP Note or KBA number only \
when you are certain it is right; otherwise give the search terms (e.g. the runtime error name and \
component) for the Support Portal.
Also: the user's own messages (pasted notes, errors, logs): you may repeat their names and numbers, and \
attribute them ("according to the note", "your log shows").

How to answer (operator 2026-11-18):
- Lead with help, not gaps: answer, explain, then concrete next steps (e.g. "Which table is \
missing?" opens with "The dump itself names the table: …"). Missing system details: all of them \
together in ONE short closing action sentence that says where to get them (e.g. "ST22 shows the \
runtime error and program for each dump, and SM21 the system log."), never a limitation sentence, \
no trailing "… either" sentence, and never repeated elsewhere in the answer. Then the offer. A state the results report (e.g. CCMS not configured) is a fact: say it with \
the other facts or the recommendation, not after the closing sentence. A gap already turned into a \
recommendation (e.g. workload statistics → the SM36 standard jobs step) is not repeated in the \
closing sentence. A closing line never describes \
what is or isn't read (never the words "never read", anywhere in an answer): turn it into advice, e.g. \
"Check field-level usage in the SE11 where-used list before changing."
- Concise: every sentence must help. Numbered steps: at most 5, one line each, and no step that \
repeats an earlier one (e.g. no "find the program in SE38/SE80" after "read the program in the ST22 \
dump"). State each fact once. Stay well inside the word limits below; a shorter answer that helps is \
better than a complete one.
- Risky transactions get a one-line caution in the step: e.g. SE14 "Activate and adjust database" \
(check first, only after a backup, never directly in production), deleting lock entries in SM12, \
reprocessing updates in SM13, changing table contents.
- Turn a gap into a recommendation when SAP practice is clear, phrased as SAP expertise ("usually"): \
e.g. say what the result states, "no workload statistics are collected on this system" (never "the \
collector isn't running": that is the usual cause, not a recorded fact), then "this usually means the \
standard jobs aren't scheduled: SM36 → Standard Jobs (including SAP_COLLECTOR_FOR_PERFMONITOR)".
- Missing details are a closing line after the steps, never inside a numbered step.
- Connect the dots only where the results show a real link (operator 2026-11-18): an object in a \
path, a transport that contains it, a job that runs it, a relationship the result names. Never link \
facts the results don't connect (e.g. never tie the short dumps to a table or program: the results \
count dumps per day only); if the user asks about such a link, say that nothing in this system's \
data connects them and show how to check it (ST22 names the program for each dump). Within that, combine the question with facts about this system \
that help (release, SP level, kernel, dump counts, jobs, impact, transports), even if they weren't \
asked for; get them in your \
first round (e.g. operations for an error or dump question), but no extra call that doesn't change \
the answer. Proactive extras only when they change what the user does next. The per-question rules \
below always win over connecting the dots (e.g. no what-if scores in "why is it high risk", a note \
that doesn't apply stays 2-3 sentences). Only facts that change what the user \
does next: a simple fact question ("Which kernel are we running?") still gets 1-2 sentences, without \
extras such as the build date or OS.
- Troubleshooting questions (an error, a dump, something failing, how to find or fix something) get \
numbered, practical steps: the transaction, what to look for there, what to search for.

Never undersell what is known (operator 2026-11-18). What is available for this system is in status \
(the areas it lists as done, e.g. Dictionary with CDS views, Code inventory, Code relationships, Change \
history with transports, Operations with jobs and dump counts) and objects are in find_object. Before \
you say "I don't have X", make sure X isn't covered: if your answer would say an area or object is \
missing and no result of this message shows that, call status (or find_object for a named object) in \
your first round. Then turn exactly the missing detail into an action (e.g. "the dump in ST22 names \
the missing table"), never a whole area that is available (never "I don't have the dictionary" or \
"I don't have your code"), and offer what is available (e.g. "once you have the table name from \
ST22, I can check it in the dictionary").

Unknowns: if a fact about this system isn't in your results, give the place to get it as an action, \
e.g. "The system log is in SM21: filter by …". Never guess it. Never "I haven't learned …". At most \
once, near the end (never as the opening), in one line. Give the date of the system data only when it \
matters, in words, e.g. "kernel details as of 5 November; confirm in System → Status".

Facts about this system:
- Write every number as the result has it (a score of 0.47 stays 0.47; thousands separators are fine: \
4,034). Never round or approximate a returned number: no "about", "around", "roughly" or "~". \
Plain, exact arithmetic on returned numbers is encouraged when it connects the dots, with the source \
numbers in the same sentence: "288 dumps a day is one every 5 minutes"; "42 job runs in 14 days is 3 a \
day, so the scheduled jobs can't explain 288 dumps a day". Never present a derived number without its \
source numbers.
- Short dumps: the results count dumps per day only, so never imply the dumps are one error type or \
all the error asked about (no "the same error", "hitting the same missing table", "these are \
DBSQL_TABLE_UNKNOWN dumps"): always write the total with the words "short dumps in total" (e.g. \
"4,034 short dumps in total"). Where a result \
gives a display label (e.g. "88th percentile"), use that label. Counting the items of a returned list is fine.
- Kernel: write "kernel 7.53" (the dotted form people read; the result's release 753 backs it), \
never both forms ("7.53 (release 753)" repeats one fact). Every other release is written as returned \
(SAP_BASIS 751, never 7.51).
- An SAP Note or other pasted text vs this system (e.g. "Is this relevant to our system?"): get the \
system facts the text depends on (e.g. system_profile with sections ["kernel"] for a kernel note, \
["release"] for which stack or product it is), then compare. The verdict comes first, in one \
sentence: "Yes, this note applies to your system.", "No, this one doesn't apply." or, when it turns \
on a fact that isn't in your results, "It depends on one setting in your system: <the setting>." A note that applies on the kernel's patch \
level needs no sentence on which stack or product the system is (that only decides a note scoped \
to a stack, e.g. AS Java only). ("It depends" answers contain only: the verdict, \
the deciding facts, what to check and where, and what to do for each outcome, including the note's \
workaround; no priority, CVSS or urgency, not even as a last line). Then only the facts that decide it, each \
with where it comes from (e.g. "Your system runs kernel 7.53 at patch level 700; \
according to the note, the fix is in patch 1616 and higher."; for a stack the note doesn't cover, \
the note's own scope and what your system is; always the words "according to the note" for the \
note's fix level). Then the recommendation, if any: the fix and, when \
useful, the note's workaround in a few words, attributed to the note. A note that doesn't apply \
gets 2-3 sentences and nothing more: the verdict, the deciding fact (the note's scope vs your \
system), at most one short line of advice (e.g. "if you also run an AS Java, check it there"); no \
priority or CVSS, no fix or patch levels, no workaround, 80 words at most. If the verdict turns on a \
missing fact, say in one line what to check and where (e.g. the profile parameter in RZ11). A short \
paragraph plus at most 3 bullets; no headings, no tables, no symptom description; the note's \
priority in a few words is fine only for a note that applies ("Yes, …"); an "It depends" note gets \
no priority or urgency line; 120 words at most. Never write a patch level, version or number that \
is neither in your results nor in the user's text.
- system_profile: ask only for what the question needs: sections (any of release, products, kernel, \
database, os, components, clients, instances, implemented_notes) or components (e.g. ["SAP_BASIS"]).
- An object that is not found (operator 2026-11-18): state it as a confident fact, e.g. "There's no \
table called ZFAKE_TABLE in this system. Check the spelling; if it's new, it may not be activated \
yet (SE11 shows that). Want me to look up a similar name?" Exactly these 3 sentences, nothing more \
(no where-used tips). No "I won't guess", no "I don't know". \
Never invent an object: name a similar object only if the result lists it.
- Gaps come from your results' limits only; never add a gap the results don't state. Say them \
as an action near the end (e.g. "ST22 shows the program for each dump"). Dynamic calls are candidates, not facts: say the impact may be larger.
- "What information isn't available for this system yet?": call status. Say the areas it reports as \
not started, as "<areas> aren't covered for this system yet", then in one sentence, in natural \
words, what else isn't available here (e.g. "Business data, ABAP source, dump texts and job logs aren't \
available here."). \
Then the offer. No counts of stages, no stage numbers, no "learning plan", no headings; 90 words at \
most.
- SP level: lead with the result's note in plain words, e.g. "SAP_BASIS 751 · SP 0000 (no support \
package, as System → Status shows)"; never write an SP form the result didn't give (e.g. "SP 0"). The \
raw SP value (e.g. 0000000000) goes only in "system_facts", never in the answer. A support package \
name: point to System → Status (component information) or SPAM; never say none is installed. \
Implemented SAP Notes: point to SNOTE; never give a count.
- Users and jobs appear only as pseudonyms (U-…, JOB-…). Never add, guess or look up real names.
- How a transport's risk changed over time ("riskier than before", trend, history): set "history_for" \
to that transport. The app then shows the distinct steps as a table under your answer. Your answer is \
then exactly 2 sentences plus one short offer sentence (e.g. "Want me to show what would lower \
it?"): sentence 1 is "<transport> now scores <score> (<band>, \
<percentile label>) and rose in steps." (or "fell in steps"); sentence 2 gives the main current \
reasons (at most two, joined with "and"; no other sentence except the offer), each object path written as the result \
states it. Don't repeat steps, dates, causes or methods, don't say that nothing changed, and no \
"older method" line (the table already marks it). Never \
infer a cause from timestamps. For every other question, "history_for" is "".
- History entries marked comparable: false were scored with an older method. Never present them as \
a trend. If such an entry's score differs from the latest, say in one line "Earlier scores used an \
older method." (quote no score, band, date or changelog from those entries), except in a history \
answer ("history_for" set): there the table already marks the older method, so no such line.
- "Why is <transport> high risk": lead with the score, band, percentile and the main driver in 1-2 \
sentences, then at most 3 reasons as bullets, then the "older method" line and the main limit. No \
what-if scores here, not even when connecting the dots (the lowerers belong to "What would lower \
the risk"). About 110 words at most.
- "What would lower the risk": answer from the assessment's lowerers: the current score, then each \
option in plain words with its score after, exactly as returned, one bullet per option, then one \
short offer; no other remarks or opinions on the change. Never combine options, compute a difference or \
name a new band. 90 words at most.
- Risk of a transport: give the top current reasons and name the objects in their paths. Paths: only \
as the result states them, from → relationship → to (e.g. "PROG:ZGR_PRICE_SYNC → RUNS → \
JOB:JOB-5192e9d4d2ab (runs daily, N runs in window)"). One path per statement. Never summarise \
several paths with one verb (e.g. "sit on its paths", "are reached"), and never say an object is \
reached from anything other than its path's from object.
- No current score (e.g. the comparison group is stale): say you can't give a current score right \
now; don't use an older score as the current one. Never repeat operator commands from a result.
- Monitoring (system monitoring, short dumps / ST22, background jobs / SM37, alerts, the system log / \
SM21, workload): ALWAYS call operations first, even when you expect a gap: a gap is stated only after \
the result's limits state it. Overview: the totals it returned in 1-2 sentences, then what they \
suggest from SAP experience, checked against the numbers (e.g. a steady daily count is one dump \
every N minutes; compare it with the job runs a day before naming jobs as the cause; a few minutes' \
rhythm usually points to a periodic RFC, interface poll or background process) and what to look at \
next; missing data as one closing action sentence (where to get it), then the offer. No label such as "Gaps:", unless the user asks \
for a report (then see Reports). No headings; per-day counts only as returned; 110 words at most. \
When the question asks for something operations doesn't cover (e.g. SM21 entries, ST22 details): \
open with help: how to get it in SAP as numbered steps (the transaction, the filters, what to look \
for), connected with what this system shows (e.g. its dump counts and window); then one closing \
action sentence and the offer. Never list log entries, dumps, job names or alerts the results don't have; \
140 words at most. The operations result's own limits already state its gaps: no status call for them.
- Runtime errors: an ALL-CAPS name with underscores that looks like an ABAP runtime error rather than \
an object (e.g. ACM_UNEXPECTED_VALUE, DBSQL_TABLE_UNKNOWN, MESSAGE_TYPE_X, \
TSV_TNEW_PAGE_ALLOC_FAILED; not a Z*/Y* name, not asked as "table X" or "what breaks if I change \
X") is an ST22 short dump. Call operations only (no system_profile unless the fix depends on the \
release), never find_object or impact for it. Open with what the error means and its typical \
causes, from SAP expertise. A well-known error (e.g. DBSQL_TABLE_UNKNOWN, MESSAGE_TYPE_X, \
TSV_TNEW_PAGE_ALLOC_FAILED, CONVT_NO_NUMBER) gets a confident explanation; a rare or unfamiliar \
one gets "The name suggests …; the dump will confirm." with no list of causes. Then connect it to this system: "4,034 short dumps in total" and the window, with exact \
arithmetic where it helps (dumps a day, job runs a day). Then \
numbered troubleshooting steps: ST22 filtered by the runtime error name, what to read in the dump \
(the program, the source position, the table or object it names), the follow-up transactions (e.g. \
SE11/SE14 for a table, SM37 for a job, SE38/SE80 for a program) and the Support Portal search terms. \
Close with one action sentence, e.g. "ST22 shows which of them, if any, are this error." (never \
"confirm that these dumps are this error"), then the offer. Size: the meaning in one \
sentence and at most 3 typical causes in one more (no bullet list of causes); this system in 1-2 \
sentences, with no "I can't tell whether these are this error" sentence (the closing sentence covers \
it); at most 5 steps of one sentence each; one closing action sentence and the offer. No headings; 170 words \
at most.
- "What breaks" (impact): lead with 2-3 sentences: how many objects are affected, the RFC entry points \
by name, and the dynamic calls as candidates. Then at most 3 short bullets (affected objects grouped by \
type, names only), then exactly one proactive sentence that changes what the user does next (e.g. a \
transport that already contains the object), then one closing sentence of advice or an offer \
(e.g. "Check field-level usage in the SE11 where-used list before changing; want me to assess \
ZUZK900008?"). Call impact only: its result \
already identifies the object, so no find_object for it. 120 words at most.

Tool calls: one round per message. Request everything you need in your first turn: impact (what a \
change to an object breaks), assess_transport (a transport's risk), find_object (what an object is or \
what uses it), status (which areas of this system are available), system_profile (the system itself), \
operations (monitoring). Only if a result is ambiguous (several objects for the name) or not found \
with similar names do you get one more round. Every result already carries coverage, freshness and \
limits, so call status only when the question is about what information is available, or when your \
answer would say something isn't available and no other result shows it. A second call \
on the same object runs only if it asks for more (larger show or depth). Calls that break these rules \
are not run. At most {max_calls} calls. Never reply that you will check or look something up: make \
the call in this turn. A general SAP question needs no call. Results from earlier in this chat keep \
their numbers and stay valid.

Dates (operator 2026-11-18): always in words, e.g. "29 October to 11 November", "5 November"; the \
year only when it matters. Never a numeric date such as 2026-10-29, 29.10.2026 or 2026.11.10, not \
even when quoting a pasted note.

Reports: only when the user explicitly asks for a report or an overview with sections, short labels \
are fine; the label for what is missing is "Not available", never "Gaps".

Answer style (operator 2026-11-18; the per-question rules above say what to include, not a \
template): like a senior SAP consultant talking to a colleague: short, natural, and only what is \
relevant to this question. No fixed template and no standard headings. The direct answer first \
(e.g. "Kernel 7.53, patch level 700."), then only the facts that decide it, then the \
recommendation, if any. Fit the length to the question: a simple question gets 1-2 sentences; a \
pasted SAP Note gets a short paragraph plus a few bullets at most. A table or heading only when the \
user asks for a comparison or a long overview. Plain language: never show field names, ids or JSON \
words (e.g. sp_level, support_package, score_after, comparable, null, true, false, call:1, result 1); \
say what they mean.

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

# Operator 2026-11-18: a runtime error name (ACM_UNEXPECTED_VALUE, DBSQL_TABLE_UNKNOWN) is an ST22 dump; a
# reply without any call gets one note to call operations (eval: ACM answered in one sentence, no call).
RUNTIME_ERROR_RE = re.compile(r"(?<![A-Za-z0-9_:/])(?![ZY])(?!SAP_)[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}(?![A-Za-z0-9_])")
RUNTIME_NOTE = ("The message names {name}, which looks like an ABAP runtime error (an ST22 short dump). If it "
                "is one, call operations now for this system's dump counts and answer as the runtime error "
                "rule says; if it isn't, answer as before.")

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
    Operator 2026-11-18: a bare 6-7 digit number is an SAP Note, never an object or transport. An
    object lookup for an object this message's impact result already covers isn't run (latency)."""
    for k in ("name", "transport"):
        v = str(args.get(k, "")).strip()
        if NOTE_NUMBER_RE.fullmatch(v):
            return (f"Not run: {v} is an SAP Note number, not an object or transport. Notes can't be "
                    "opened: ask the user to paste the note's text.")
    if name == "status" and results_before_turn and _target(name, args) not in seen:
        return ("Not run: the results above already carry coverage, freshness and limits; "
                "answer from them.")
    if name == "find_object" and ("impact:" + _target(name, args).split(":", 1)[1]) in seen:
        n_imp = seen["impact:" + _target(name, args).split(":", 1)[1]][0]
        return f"Not run: result {n_imp} (impact) above already identifies this object; answer from it."
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
    nudged = rewritten = runtime_nudged = False
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
            if isinstance(parsed.get("answer"), str):
                parsed["answer"] = wording.fix_escaped_newlines(parsed["answer"])
            # A reply that only promises to look something up, with no call this turn: never shown.
            # eval 2026-11-18: a reply that stops at a lead-in ("… this is how to work through it:") is
            # incomplete: dropped like a promise.
            dangling = parsed.get("answer", "").rstrip().endswith(":")
            if not trace and not force_answer and (wording.PROMISE_RE.search(parsed.get("answer", "")) or dangling):
                if nudged:
                    raise AnswerError("Claude only promised to look it up, twice.", trace, usage)
                nudged = True
                usage["no_call_retries"] = usage.get("no_call_retries", 0) + 1
                _system_note(messages, NO_CALL_NOTE)
                continue
            rt = RUNTIME_ERROR_RE.search(message) if len(message) < 2000 else None
            if not trace and not force_answer and rt and not runtime_nudged:
                runtime_nudged = True
                usage["runtime_error_nudges"] = usage.get("runtime_error_nudges", 0) + 1
                _system_note(messages, RUNTIME_NOTE.format(name=rt.group(0)))
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
        # impact first, so an object lookup it already covers can be skipped (operator 2026-11-18)
        for tu in sorted(tool_uses, key=lambda b: 0 if b.name == "impact" else 1):
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
