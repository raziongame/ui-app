# Northwind SAP Assistant: sample partner app for Graphene

This repo is a **sample partner application**, built by the Graphene team to show how a
partner's own AI application plugs into Graphene. It is **not** Graphene and **not** a real
partner. Say "sample partner app" in the UI, docs and demo.

## What this app is (and is not)

- **Is:** a small web app with a chat UI. It takes a user's question, lets Claude call
  Graphene's tools, and explains the answer in plain language, with the evidence shown.
- **Is not:** a source of facts. Every SAP fact (objects, relationships, risk scores, counts,
  transports, jobs, dumps) comes from Graphene. This app never computes, changes or
  guesses a fact.

## Hard boundaries (non-negotiable)

1. **Only one way into Graphene: its MCP server.** Start it over stdio with
   `/home/grpadm/graphene/.venv/bin/graphene-mcp`. Never import Graphene code, never read its
   files, never connect to Neo4j, Postgres or SAP, never edit `~/graphene`.
2. **No SAP access of any kind.** This app has no SAP credentials and calls nothing in SAP.
3. **Facts only from tool results.** Every object name, transport, number and date in an answer
   must appear in a Graphene tool result from this question. Unknown objects: say
   "not found in the learned system". Never recompute or round a number differently.
4. **Say what Graphene doesn't know.** Pass on its `limits`, `coverage` and `freshness`
   (e.g. stages not learned yet, dynamic calls that are candidates, not facts).
5. **Secrets:** `ANTHROPIC_API_KEY` lives only in this repo's `.env` (gitignored, chmod 600).
   Commit `.env.example` with a placeholder only. Never print, log or send the key to the
   browser. Never paste secrets into chat.
6. **Privacy:** Graphene results already carry pseudonyms (U-…, JOB-…). Don't add, guess or
   look up real user or job names. Logs hold questions, tool calls and timings, never the key.

## Stack

- Python 3.12 venv (uv), FastAPI on **127.0.0.1:8080** (localhost only), `anthropic` SDK,
  `mcp` Python SDK (stdio client).
- Model: `claude-sonnet-5-5`. Max 8 tool calls per question, with timeouts.
- UI: one self-contained HTML page served by the app (inline CSS/JS, no CDNs, no external
  calls). Dark and light mode. Readable on a projector at 1920×1080.
- Reached from the operator's laptop with
  `ssh -p 2222 -L 8080:127.0.0.1:8080 grpadm@127.0.0.1`, then http://localhost:8080.

## Answer contract (what every answer carries)

`answer` (plain language), `claims[]` each with `evidence_ids`, `tool_trace` (tool, arguments,
short result, ms), `coverage`, `freshness`, `limits`, and a `grounding` result.
For "how did the risk change" questions also `history`: the distinct score steps (date, score from → to,
cause, older engine) built by the app straight from Graphene's history, never written by the model
(operator, 2026-11-16).

**Grounding check** runs after every answer: any object/transport name or number in the answer
that is not in this question's tool results is flagged in the UI (yellow), never hidden.

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
4. `make eval` runs the golden questions against the real API: run it before tagging, and
   report the cost estimate first.
5. Small commits on a branch, merge when the operator approves, tag `partner-demo-vN`.

## Golden questions (start with these)

1. "What breaks if I change ZS4_IOT_DATA?" → 9 affected objects, 2 RFC entry points,
   1 dynamic call (candidate).
2. "Why is ZUZK900008 high risk?" → 0.470, high, 88th percentile, its top reasons.
3. "Why is ZUZK900006 riskier than before?" → 0.345, the daily job reached through
   ZGR_PRICE_SYNC (runs daily, 12 runs in window).
4. "What uses table ZFAKE_TABLE?" → not found in the learned system, no guessing.
5. "What does Graphene not know yet?" → stages not learned (from status), stated limits.

Expected numbers come from Graphene; if Graphene's numbers change after a learn run, update
the expectations from Graphene's own output, never by hand-guessing.

## Ask the operator before

- adding a dependency or any network call other than the Claude API,
- anything that would touch `~/graphene`, SAP, or another machine,
- changing the model, the port, or the answer contract.
