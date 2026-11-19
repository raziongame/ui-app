# Graphene by NiraamAI: SAP assistant on the Graphene engine

Internally (repo, docs, commits) this is a **demo application built by the Graphene team** on top of
the Graphene engine, reached **only through its MCP server**, with every boundary below.

**One brand** (operator 2026-11-17, final requirements 2026-11-18): **Graphene by NiraamAI**, served on `/` (NiraamAI logo
`static/brand/logo.png`; colours navy #003858, ocean blue #2A82B4, teal #1298B0, cyan #4AC2E0; text
meets WCAG AA in light and dark, `tests/test_ui.py`). No other skins, no `/direct`.

## What this app is (and is not)

- **Is:** a full SAP expert assistant in a chat UI that looks and feels like ChatGPT: left sidebar
  (logo + "Graphene", "New chat", chat history, Delete chat), the conversation (user messages as
  right-side bubbles, answers as markdown with headings, lists and tables), a multi-line input at the
  bottom (Enter sends, Shift+Enter is a new line, pastes up to 200,000 characters). The empty chat
  shows only "How can I help with your SAP system?". It answers questions about the learned system,
  general SAP questions, and questions about text the user pastes (SAP Notes, errors, logs). It already
  knows the system through the engine's tools and **never asks the user for a system detail a tool can
  give** (versions, kernel, components, objects, transports, jobs, …).
- **Not in scope:** web search, file uploads (later).
- **Is not:** a source of facts about the system. Every fact about THIS system (versions, kernel,
  components, objects, relationships, transports, jobs, dumps, risk, counts, dates) comes from
  Graphene's tools. This app never computes, changes or guesses such a fact.
- **Not in the UI:** suggestion chips, a "What I've learned" sidebar, a status dot, "How do I know?",
  grounding badges, timings, tool names, field names, ids, JSON words. Sources, grounding results
  and the trace stay in the server log and the `?debug=1` view (for us only). While an answer runs,
  one neutral grey line ("Checking your system…", "Looking up ZS4_IOT_DATA…") that disappears.

## Hard boundaries (non-negotiable)

1. **Only one way into Graphene: its MCP server.** Start it over stdio with
   `/home/grpadm/graphene/.venv/bin/graphene-mcp`. Never import Graphene code, never read its
   files, never connect to Neo4j, Postgres or SAP, never edit `~/graphene`.
2. **No SAP access of any kind.** This app has no SAP credentials and calls nothing in SAP.
3. **Facts about this system only from tool results.** Every name, number and date in a statement
   about THIS system must appear in a Graphene result from this chat, or in the user's own messages
   (names and numbers from a pasted note or log may be repeated, attributed to it). General SAP
   knowledge is allowed, phrased as general knowledge, never as a fact about this system. Unknown
   objects, as a confident fact: "There's no table called ZFAKE_TABLE in this system. Check the
   spelling; if it's new, it may not be activated yet (SE11 shows that). Want me to look up a similar
   name?" (never invent an object; a similar name only if the engine lists it). A system
   fact that isn't in the results: give the place to get it as an action ("The system log is in
   SM21: filter by …"), never a refusal. Never round a returned number; plain exact arithmetic on
   engine numbers is allowed with the source numbers in the same sentence ("288 a day is one every 5
   minutes"; grounding backs a ± × ÷ of that sentence's numbers or a time constant, logged as
   `derived`). Kernel: "kernel 7.53" (dotted form; the engine's release 753 backs it; never "7.53
   (release 753)", operator 2026-11-18); every other release as returned (SAP_BASIS 751, never 7.51). A note vs this system: show both
   values ("your kernel … patch 700; according to the note, the fix is in patch 1616") and say
   "according to the note".
4. **Turn what isn't known into an action, when it matters.** Pass on Graphene's `limits`,
   `coverage` and `freshness` as one closing action line (where to get it: "ST22 shows the program for
   each dump"; dynamic calls are candidates, not facts) when they change the answer or the question is
   about them; never a limitation sentence, never "What I don't know", never what is "read" or
   "recorded". A date for the system data only when it matters
   ("kernel details as of 5 November; confirm in System → Status").
5. **Secrets:** `ANTHROPIC_API_KEY` lives only in this repo's `.env` (gitignored, chmod 600).
   Commit `.env.example` with a placeholder only. Never print, log or send the key to the
   browser. Never paste secrets into chat.
6. **Privacy:** Graphene results already carry pseudonyms (U-…, JOB-…). Don't add, guess or
   look up real user or job names. Logs hold questions, tool calls and timings, never the key.

## Stack

- Python 3.12 venv (uv), FastAPI on **127.0.0.1:8080** (localhost only), `anthropic` SDK,
  `mcp` Python SDK (stdio client).
- Model: `claude-sonnet-5-5`. Max 8 tool calls per question, with timeouts.
- UI: one self-contained page (`app/static/index.html`, inline CSS/JS and a small markdown renderer
  that escapes everything first; no CDNs, no external calls). Dark and light mode.
- Chats are stored on the server per browser (random id in an HttpOnly cookie), in SQLite
  (`data/chats.sqlite3`, gitignored), kept until the user deletes them. Earlier turns go to the model
  as context, each question with the engine results behind its answer (operator 2026-11-19); results
  are stored per chat and keep their numbers, so follow-ups may cite them and never disown a backed answer.
- Graphene 1.38.0 (handshake, 2026-11-19; operations adds ST02 buffers, CCMS open alerts, the instance start). `system_profile`: only the sections or components a
  question needs (v18 filters).
- Runs as the systemd user service `sap-assistant` (README "Service").
- Reached from the operator's laptop with
  `ssh -p 2222 -L 8080:127.0.0.1:8080 grpadm@127.0.0.1`, then http://localhost:8080.

## Voice (operator 2026-11-18, final)

**The most important principle (operator 2026-11-18):** the user must feel a capable SAP AI assistant
that gets their task done and improves their accuracy, like ChatGPT/Claude, but knowing their system.
1. Open by doing the task (the answer, the analysis or the first concrete step); never open with "I
   can't", "I don't have", "isn't permitted", "isn't recorded".
2. Do as much of the task as possible with system facts + SAP expertise; turn what's missing into an
   action ("Paste the code and I'll review it", or the exact transaction), never a refusal sentence.
3. Accuracy: system facts exact, from the engine; SAP expertise confident and practical; point out
   the risks the user may have missed.
4. End with one useful offer of what to do next (check impact, assess the transport, review pasted
   code/dump/note).
5. Never mention the tool, permissions, what is "read" or "recorded", or how lookups work.
The eval fails any answer whose first sentence is a refusal or limitation, or without an offer in its
last two sentences; `app/wording.py` flags refusal sentences, tool talk and "recorded/permitted".

Natural and confident, short and clear, about the user's system ("Your system runs …", "The system
log is in SM21: …"). **Never talks about itself or how it knows:** no
"I've learned", "learned", "learning run", "as I learned it", "my (kernel) data", "trained", "AI
model", "engine", "Graphene's tools", tool or field names, JSON words. Asked what it is or how it
knows: one line, "I'm here to help with your SAP system; what would you like to check?", then carry
on. `app/wording.py` enforces this in the silent check (rewrite once, then hold back) and in the eval.

**Proactive SAP expert (operator 2026-11-18, latest):** two layers. Facts about THIS system: strictly
from the engine; exact arithmetic on them is encouraged when it connects the dots, with the source
numbers alongside ("42 job runs in 14 days is 3 a day, so the jobs can't explain 288 dumps a day").
Dumps: "4,034 short dumps in total", never implying one error type until the engine records error
names. Risky transactions (SE14 adjust, SM12, SM13, table edits) get a one-line caution. Clear SAP
practice turns a gap into a recommendation (no workload aggregates + few jobs → SM36 → Standard Jobs,
SAP_COLLECTOR_FOR_PERFMONITOR). Steps never repeat each other; each fact once. Well-known errors get a
confident explanation; rare ones "The name suggests …; the dump will confirm." SAP
expertise (what errors mean, typical causes, troubleshooting, transactions, Support Portal search
terms, best practice): used freely; "typically", "usually", "a common cause is" are fine; specific
SAP Note numbers only when certain, else search terms. Lead with help, never open with a gap; all
missing system details in ONE short sentence near the end. Connect the dots (release, dump counts,
jobs, impact) when it helps. Troubleshooting gets numbered steps (≤ 5, one sentence each). An
ALL-CAPS runtime error name with no call in the reply gets one app note to call operations.

**Answer style (operator 2026-11-18, replaces any fixed format):** a senior SAP consultant talking to
a colleague: short, natural, only what's relevant to this question. No fixed template, no standard
headings. The direct answer first ("Yes, this note applies to your system." / "No, this one doesn't
apply." / "Kernel 7.53 (release 753), patch level 700."), then only the facts that decide it, then the
recommendation, if any. Simple questions: 1–2 sentences; a pasted SAP Note: a short paragraph plus a
few bullets at most, no CVSS, tables or long descriptions unless asked. A missing fact that changes
the answer: one line, what to check and where. Golden expectations check content (verdict first, key
facts, recommendation) and a max length per question, never a layout.
**Never undersell what's known** (operator 2026-11-18): before "I don't have X", check what is
available (status: Dictionary with CDS views, Code inventory, Code relationships, Change history with
transports, Operations with jobs and dump counts; find_object for objects) and turn exactly the
missing detail into an action ("the dump in ST22 names the missing table"), never a whole available
area. Connect facts only where the engine shows a link (never tie the dumps to a table or program).
**Dates in words** ("29 October to 11 November"), never 2026-10-29. Requested reports may use short
labels; what is missing is "Not available", never "Gaps" (`app/wording.py` flags both).
SAP Note numbers (6–7 digits, "note N") are never looked up: "That looks like SAP Note N. Paste its
text here and I'll check it against your system."

## Answer contract (internal: server, log, `?debug=1`)

`answer` (markdown), `system_facts[]`: every statement about THIS system, each citing tool results by
number (`results: [N]`); the app fills in Graphene's real `evidence_ids` from those results (the model
never types ids). Plus `tool_trace` (tool, arguments, short result, ms), `coverage`, `freshness`,
`limits`, `grounding`, and the presentation outcome. For "how did the risk change" questions also
`history`: the distinct score steps (date, score from → to, cause, older method), rendered by the app
as a table under the answer straight from Graphene's history, never written by the model.

**Silent grounding** (operator 2026-11-17) checks only statements about this system: the declared
system facts in full; in the answer, system-only names (Z*/Y* objects, transports, pseudonyms) and the
numbers/dates of sentences about this system. Backing = this chat's Graphene results + the user's own
messages (+ the dotted kernel form). Not backed → the model rewrites the answer once (the draft is
never shown); still not backed → that sentence or table row is held back with a short note; nothing
left → "I'm not certain about that one. Can you rephrase or name a specific object?". The outcome
(passed, rewritten, held back, fallback) goes to the log and `?debug=1`, never to the UI.

## How this app improves: the operator's testing loop

The operator (Razi) tests the app and reports what's wrong. Every report goes to **exactly one**
place:

| The problem is… | Fix it in… |
|---|---|
| wording, tone, layout, UI, flow, how evidence is shown, a slow or confusing step | **this repo** |
| a **fact** is wrong or missing (wrong object, wrong count, missing relationship, wrong risk) | **Graphene**, not here: write it up in `GRAPHENE_ISSUES.md` (question, what Graphene returned, what's expected) and tell the operator. Never work around a wrong fact in this app. |

Rules for each fix:
1. Log it in `TEST_LOG.md`: date, question, expected, got, verdict, owner (app or Graphene),
   fix commit.
2. Add or update a case in `tests/golden_questions.yaml` so it can't come back.
3. `make test` (mocked Claude, real Graphene MCP) must pass before any commit.
4. **Eval cost rule (operator 2026-11-17, figures 2026-11-18).** The full eval (`make eval`: every
   golden question, 28 today, **~$1.45–1.70**) runs **only before a tag**. Its cap scales with its
   size: **$0.065 a question, rounded up to the next 10 cents** (28 → $1.90), unless `--max-cost` is
   given. While
   fixing, run only the affected questions: `make eval-cases CASES="id ..."`, capped at **$1 per run**
   (the runner stops before it would pass the cap). Report the cost estimate first
   (`make eval-estimate [CASES=…]`) and the actual cost after. `python eval/run_eval.py --rescore
   eval/results/<file>.json` re-checks saved answers with the current checks, no API calls.
   **Soft limits (operator 2026-11-18):** word and sentence-count limits warn up to 20% over and fail
   only above that. Facts, grounding, applies verdicts, priority/urgency and the offer rule stay strict.
   **Tag rule (operator 2026-11-18):** tag when the full eval passes, or when at most **3** cases fail
   and every failure is style only (length or sentence/gap count). Zero tolerance for anything else:
   facts, grounding, applies/doesn't-apply verdicts, priority/urgency (and tools, content):
   any of those → stop and show the operator. Failed cases may be re-run and the combined latest
   answers re-scored; the runner prints the verdict ("TAG OK per rule" / "NO TAG").
   **Latency (operator 2026-11-18):** one case over its time limit (10 s, or the case's own) but up to
   15 s is a soft warning; above 15 s, or 2+ cases over their limit in one run, blocks the tag.
5. Small commits on a branch, merge when the operator approves (after a look in the browser), tag
   `graphene-ui-vN` (earlier: `partner-demo-vN`, `whitelabel-v1`).

## Golden questions (start with these)

`tests/golden_questions.yaml` has the full list (Q1–Q10) and the test-only cases.

1. "What breaks if I change ZS4_IOT_DATA?" → 9 affected objects, 2 RFC entry points,
   1 dynamic call (candidate).
2. "Why is ZUZK900008 high risk?" → 0.470, high, 88th percentile, its top reasons.
3. "Why is ZUZK900006 riskier than before?" → 0.345, the daily job reached through
   ZGR_PRICE_SYNC (runs daily; 14 runs in window since the 2026-11-18 learn run, was 12).
4. "What uses table ZFAKE_TABLE?" → "There's no table called ZFAKE_TABLE in this system. Check the
   spelling; if it's new, it may not be activated yet (SE11 shows that). Want me to look up a similar
   name?" (operator 2026-11-18: a confident fact, no "I won't guess")
5. "What information isn't available for this system yet?" (was "What haven't you learned yet?",
   changed 2026-11-18) → "Configuration, Access, … aren't covered for this system yet" (from status)
   + what else isn't available, in natural words (never "never read"); no stage counts or numbers.
6–9. SAP_BASIS version, what would lower ZUZK900008's risk, system monitoring, SM21 (the stated gap).
10. SAP Note 3778530 (CVE-2026-76964), pasted (`tests/fixtures/note_3778530_excerpt.txt`, used exactly),
   + "Is this relevant to our system?" → first "Yes, this note applies to your system."; kernel 7.53
   patch 700 from the engine vs the fix in 1616 "according to the note"; recommend
   patch 1616 or higher (the note's workarounds in a few words). No headings, tables or CVSS; ≤ 130
   words; no invented patch levels.
11. Identity ("What are you?", "How do you know all this about my system?") → the one line, no tool
   call. "Which kernel are we running?" → from the engine, never asked of the user, 1–2 sentences.
12. SAP Note numbers alone ("Is SAP Note 3778530 relevant to our system?", "3778530") → the
   paste-its-text line, no lookup.
13. **Synthetic** notes (`tests/fixtures/SYNTHETIC_*`, not real SAP Notes; see `tests/fixtures/README.md`):
   AS Java-only note → "No, this one doesn't apply" (engine: ABAP, SAP_BASIS 751, S/4HANA);
   note that applies only with `icm/HTTP/support_http2` = TRUE → "It depends on one setting in your
   system: …",
   kernel 700 vs the note's 1210, check the parameter in RZ11.
14. Runtime error names (ACM_UNEXPECTED_VALUE, DBSQL_TABLE_UNKNOWN: ALL CAPS with underscores, not
   Z*/Y*, not asked as an object) → an ST22 short dump: the meaning and typical causes (SAP
   expertise), this system's dump counts (since the 2026-11-18 learn run: 4034 dumps, 4–17 November,
   288 or 289 a day; 42 job runs), numbered steps (ST22, SE11/SE14, SM37, STMS, Support Portal); the
   missing error name per dump (G-10) once, at the end. Also "Show me ST22 details" / "Show me SM21
   details": how to work through it as steps, connected with the dump counts.
15. "What information is available for this system?" → dictionary with CDS views, code, code
   relationships, transports, jobs, dump counts (from status). "DBSQL_TABLE_UNKNOWN keeps coming up.
   Which table is missing?" → "which table was missing at runtime isn't recorded", ST22, then the
   dictionary check offered. "Give me a short monitoring report, with sections." → labels OK, "Not
   available", never "Gaps".
16. Buffers, CCMS / RZ20 alerts, restarts (operator 2026-11-19) → operations: "How are the SAP buffers
   doing?" → 13 buffers, 0 swaps, lowest IRBD 49.66% (Graphene's value is the truth; hit ratios change over
   time), ST02 for each buffer; "Are there any red alerts in RZ20?" → 10 red of 23 open (Performance
   Overview), highest response time 9605 ms vs 3000 at 15:52 on 1 November, the day of the 10:05 restart
   (the link from engine times; the cause as SAP expertise: compiling on first use → SGEN after restarts
   and kernel changes), collector job via SM36 → Standard Jobs, no ST03N; start time 1 November 10:05 (UTC+1), 18.3 days, ST02 to confirm, never
   SM51. Restart → buffers in one chat must never retract the backed restart answer: earlier turns go to the
   model with their results, and the answers are never disowned unless a new result contradicts them.

Expected numbers come from Graphene; if Graphene's numbers change after a learn run, update
the expectations from Graphene's own output, never by hand-guessing.

## Ask the operator before

- adding a dependency or any network call other than the Claude API,
- anything that would touch `~/graphene`, SAP, or another machine,
- changing the model, the port, or the answer contract.
