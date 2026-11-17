# SAP Assistant: white-label demo app on the Graphene engine

Internally (repo, docs, commits) this is still a **demo application built by the Graphene team** on
top of the Graphene engine, reached **only through its MCP server**, with every boundary below. It is
not Graphene itself.

**The UI is white-label** (operator 2026-11-17, investor demo): two skins from `brands.yaml`, the
same engine behind both:

| Skin | Path | Name | Product name "Graphene" |
|---|---|---|---|
| partner | `/` | Cloudbox · SAP Assistant (text logo, warm graphite/amber theme) | **never**, anywhere: page, title, answers, sources, progress, errors |
| direct | `/direct` | Graphene, "by NiraamAI" (logo `static/brand/logo.png`, navy/ocean/teal/cyan) | it is the product name |

Neither skin says "sample partner app" or "demo by the Graphene team", or shows MCP, tool names,
field names, ids or JSON words (e.g. sp_level, null, true/false, call:1). The browser gets only
neutral progress lines and the checked answer (`app/present.py`); the full internal contract stays
in the server and the log. Colours: text meets WCAG AA in both modes (`tests/test_brand.py`).

## What this app is (and is not)

- **Is:** a small web app with a chat UI. It takes a user's question, lets Claude call
  Graphene's tools, and explains the answer in plain language, in the skin's first-person voice,
  with a collapsed "How do I know?" link for the sources.
- **Is not:** a source of facts. Every SAP fact (objects, relationships, risk scores, counts,
  transports, jobs, dumps) comes from Graphene. This app never computes, changes or
  guesses a fact.

## Hard boundaries (non-negotiable)

1. **Only one way into Graphene: its MCP server.** Start it over stdio with
   `/home/grpadm/graphene/.venv/bin/graphene-mcp`. Never import Graphene code, never read its
   files, never connect to Neo4j, Postgres or SAP, never edit `~/graphene`.
2. **No SAP access of any kind.** This app has no SAP credentials and calls nothing in SAP.
3. **Facts only from tool results.** Every object name, transport, number and date in an answer
   must appear in a Graphene tool result from this question. Unknown objects, in the voice:
   "I don't know a table called ZFAKE_TABLE in this system, and I won't guess." Never recompute
   or round a number differently.
4. **Say what isn't known.** Pass on Graphene's `limits`, `coverage` and `freshness` in the voice
   (e.g. "I haven't learned the system log (SM21) yet"; dynamic calls are candidates, not facts).
   "Next on my learning plan" / "learning next" only for stages Graphene's status lists as not
   started; a stated gap inside a learned stage is just "I haven't learned … yet".
5. **Secrets:** `ANTHROPIC_API_KEY` lives only in this repo's `.env` (gitignored, chmod 600).
   Commit `.env.example` with a placeholder only. Never print, log or send the key to the
   browser. Never paste secrets into chat.
6. **Privacy:** Graphene results already carry pseudonyms (U-…, JOB-…). Don't add, guess or
   look up real user or job names. Logs hold questions, tool calls and timings, never the key.

## Stack

- Python 3.12 venv (uv), FastAPI on **127.0.0.1:8080** (localhost only), `anthropic` SDK,
  `mcp` Python SDK (stdio client).
- Model: `claude-sonnet-5-5`. Max 8 tool calls per question, with timeouts.
- UI: one self-contained HTML template (`app/static/index.html`, inline CSS/JS, no CDNs, no external
  calls) rendered per skin from `brands.yaml`; the template itself names no product. Single-column
  chat, big input at the bottom, suggestion chips only on an empty chat, "What I've learned" sidebar
  (from Graphene's `status`: ✓ per learned area, the rest "learning next", "last learned … ago"; no
  metrics the engine didn't give), status dot "Connected to <SID>". Dark and light mode. Readable on
  a projector at 1920×1080.
- Runs as the systemd user service `sap-assistant` (README "Service").
- Reached from the operator's laptop with
  `ssh -p 2222 -L 8080:127.0.0.1:8080 grpadm@127.0.0.1`, then http://localhost:8080.

## Voice (both skins)

First person, natural, confident, short and clear: "I've learned", "I know this system", "I haven't
learned … yet". Never "trained", "training", "fine-tuned", "trained model" or "our own AI model".

## Answer contract (internal: server and log; what every answer carries)

`answer` (plain language), `claims[]` each with `evidence_ids` (the model cites results by number,
`results: [N]`; the app fills in Graphene's real ids from those results, operator 2026-11-17), `tool_trace` (tool, arguments,
short result, ms), `coverage`, `freshness`, `limits`, and a `grounding` result. The skin shows only
the answer, "How do I know?" (the claims as sources in SAP terms, plus which areas were learned how
long ago) and, for history questions, the steps table.
For "how did the risk change" questions also `history`: the distinct score steps (date, score from → to,
cause, older engine) built by the app straight from Graphene's history, never written by the model
(operator, 2026-11-16).

**Grounding check** runs after every answer (silently, operator 2026-11-17): if any object/transport
name, number or date in the answer or its sources is not in this question's tool results, or the
wording rules are broken (product name in the partner skin, "trained", tool names, JSON words), the
skin shows "I'm not certain about that one. Can you rephrase or name a specific object?" instead.
The reasons go to the log (`presentation`), never to the UI. A source with a field name is left out.

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
4. **Eval cost rule (operator 2026-11-17).** The full eval (`make eval`: every golden question in
   both skins, ~$0.9) runs **only before a tag**. While fixing, run only the affected questions:
   `make eval-cases CASES="id ..."`, capped at **$1 per run** (the runner stops before it would pass
   the cap). Report the cost estimate first (`make eval-estimate [CASES=…]`) and the actual cost after.
5. Small commits on a branch, merge when the operator approves, tag `whitelabel-vN` (before 2026-11-17: `partner-demo-vN`).

## Golden questions (start with these)

Each runs in both skins (`make eval`); `tests/golden_questions.yaml` has the full list (Q1–Q9).

1. "What breaks if I change ZS4_IOT_DATA?" → 9 affected objects, 2 RFC entry points,
   1 dynamic call (candidate).
2. "Why is ZUZK900008 high risk?" → 0.470, high, 88th percentile, its top reasons.
3. "Why is ZUZK900006 riskier than before?" → 0.345, the daily job reached through
   ZGR_PRICE_SYNC (runs daily, 12 runs in window).
4. "What uses table ZFAKE_TABLE?" → "I don't know a table called ZFAKE_TABLE in this system, and
   I won't guess."
5. "What haven't you learned yet?" → stages not learned (from status), stated limits.
6–9. SAP_BASIS version, what would lower ZUZK900008's risk, system monitoring, SM21 (the stated gap).

Expected numbers come from Graphene; if Graphene's numbers change after a learn run, update
the expectations from Graphene's own output, never by hand-guessing.

## Ask the operator before

- adding a dependency or any network call other than the Claude API,
- anything that would touch `~/graphene`, SAP, or another machine,
- changing the model, the port, or the answer contract.
