# Graphene issues found by the sample partner app

Facts (and fact-level gaps) that are Graphene's to fix. The app never works around them.
Each entry: question, what Graphene returned, what's expected. Graphene `1.30.0` over MCP stdio.

---

## G-1 · First `assess_transport` on a new MCP session takes 43–59 s (warm: ~0.4 s) — RESOLVED

- **Resolved in Graphene demo-ready-v10** (re-measured 2026-11-14 on a fresh session): first
  `assess_transport ZUZK900008` **544 ms** (warm 427 · 441), `comparison_version.source: baseline`,
  no stale notice. Guarded by `tests/test_latency.py` (< 3 s).

- **Found:** 2026-11-14, while measuring tool latency on one long-lived session (`make latency`).
- **Question:** any risk question, e.g. "Why is ZUZK900008 high risk?", when it is the first assessment
  since the graphene-mcp process started.
- **What Graphene returned:** correct results, but the first call is slow. Graphene's own result explains it:
  `comparison_version.notice`: "risk history is stale: built on 2026-11-12 … built without storing it
  (after L4 v8 (since:2026-11-01), 41 s, once a day per reader process)", `assess_seconds: 41.34`.
- **Timings** (ms, one session each, same machine):

  | call | run 1 cold | run 1 warm | run 2 cold | run 2 warm |
  |---|---|---|---|---|
  | `assess_transport ZUZK900008` (first assess) | **58 668** | 570 · 383 · 406 | **43 257** | 376 · 449 |
  | `assess_transport ZUZK900006` | 414 | 390 · 360 · 362 | 400 | 368 · 372 |
  | `impact ZS4_IOT_DATA` | 1 093 | 441 · 385 · 651 | 551 | 506 · 449 |
  | `find_object ZS4_IOT_DATA` | 2 618 | 146 · 149 · 272 | 504 | 202 · 182 |
  | `status` | 91 | 139 · 88 · 119 | 142 | 105 · 100 |
  | `operations` | 65 | 62 · 64 · 77 | 85 | 103 · 68 |

  (Separate probe, fresh process: first `impact ZS4_IOT_DATA` took 23 877 ms.)
- **Warm session:** no `impact` or `assess_transport` call is over 3 s. The app keeps one session open
  for all questions, so users pay the cold cost once per app start (or once a day).
- **Expected:** the first assessment answers in ~1 s too, e.g. by storing the built risk history
  (the notice says it is built "without storing it") instead of rebuilding it per reader process.
  Also: the history is reported as stale (built 2026-11-12).

## G-2 · No earlier risk score to answer "riskier than before" — RESOLVED

- **Resolved in demo-ready-v10:** `result.previous` (score, band, assessed_at, engines, baseline_id) and
  `result.changes_since_previous`. For ZUZK900006: previous 0.345 / moderate on 2026-11-12, no changes —
  so it is **not** riskier than before; the golden case now expects the app to say so.

- **Found:** 2026-11-14, golden question 3.
- **Question:** "Why is ZUZK900006 riskier than before?"
- **What Graphene returned:** `score 0.345`, `band moderate`, reasons (incl. "reaches periodic job
  JOB:JOB-5192e9d4d2ab in 1 hop (runs daily, 12 runs in window)" via `PROG:ZGR_PRICE_SYNC`), and
  `comparison_version.since_last`: "nothing changed since the last assessment (2026-11-12 14:44)".
  There is no previous score, band or reason list to compare against.
- **Expected:** to answer "riskier than before", `assess_transport` should return the previous
  assessment's score/band (and what changed), or say explicitly that it has none. Today the app can
  only say "Graphene did not return an earlier score".

## G-3 · Percentile is a fraction (0.8833); the golden expectation says "88th percentile" — RESOLVED

- **Resolved in demo-ready-v10:** `assessment.percentile_label: "88th percentile"`; the app uses it.

- **Found:** 2026-11-14, golden question 2.
- **Question:** "Why is ZUZK900008 high risk?"
- **What Graphene returned:** `percentile: 0.8833`.
- **Expected (CLAUDE.md):** "88th percentile". The app may not convert or round numbers, so it shows
  0.8833. If "88th percentile" is the wording users should see, Graphene should return it (e.g. a
  `percentile_text`), or the golden expectation should change to 0.8833. Operator to decide.

## G-4 · MCP handshake reports version 0.1.0 (Graphene is v11 / 1.31.0) — RESOLVED

- **Resolved 2026-11-14 (same day):** the handshake now reports `1.31.0`; the app's health shows it.

- **Found:** 2026-11-14, after Graphene v11 was installed; the app's health page shows the version
  from the MCP `initialize` handshake.
- **What Graphene returned:** `serverInfo: {"name": "graphene", "version": "0.1.0"}` (raw handshake,
  no app code involved). Before v11 it returned `1.30.0`, even for demo-ready-v10.
- **Expected:** `1.31.0`, so partners can tell which Graphene build answered. The app shows what the
  handshake says and does not override it.

## G-5 · Assessment history has no cause per step — RESOLVED

- **Resolved 2026-11-16 (v12 data):** each history entry has `comparable`, `same_as_previous`,
  `changes_from_previous` (e.g. `new affected object JOB:JOB-5192e9d4d2ab via RUNS, learned_in L5`;
  `impact engine impact-3 → impact-4` with its `changelog` line) and `engine_changes_since`.
- **Open question for the operator:** Graphene marks the 0.3075 and 0.3225 entries of ZUZK900006
  `comparable: false` (only impact-4 entries are comparable), so the app shows them as steps with
  their causes, labelled "older engine", not as a comparable trend. If they should count as
  comparable, that is a Graphene change.

- **Found:** 2026-11-14, golden question 3 on Graphene v11.
- **Question:** "Why is ZUZK900006 riskier than before?"
- **What Graphene returned:** `result.history` (23 entries: assessed_at, score, band, engines,
  affected). Steps: 0.3075 / 1 affected (from 2026-11-10T12:58:39) → 0.3225 / 2 affected
  (2026-11-12T12:53:47) → 0.345 / 2 affected, engines impact-4 (from 2026-11-12T13:06:32).
  `changes_since_previous: []`. Nothing says *why* each step happened.
- **Expected (operator):** "0.3225 after L5 (2 affected)" and "0.345 (periodic-job rule, impact-4)".
  The only link to L5 is a timestamp in `status` (journal: L5 v2 passed 2026-11-12T12:52:48);
  the app must not infer a cause from timestamps. Please add a per-step cause to each history entry
  that changes score (e.g. `"change": "new affected object JOB:JOB-5192e9d4d2ab via L5 v2"`,
  `"change": "periodic-job rule (impact-4)"`).
- **Related:** `coverage.this_answer_uses` for this answer lists L1–L4 only, although the current
  reason "reaches periodic job … (runs daily, 12 runs in window)" comes from L5 operations.

## G-6 · Risk comparison group is stale: no risk score for any transport — RESOLVED

- **Resolved in demo-ready-v13 (2026-11-16 11:06 baseline, refreshes daily at 00:10):** comparison group
  `current`; ZUZK900008 0.47 / high / "88th percentile", ZUZK900006 0.345 / moderate / "66th percentile".

- **Found:** 2026-11-16, golden questions 2 and 3 (and `make test`: 5 failures, all from this).
- **What Graphene returned:** `assess_transport` → `result.assessment: null`,
  `comparison_group: {"status": "stale", "reason": "risk history is stale: built on 2026-11-14,
  quality signals count days", "fix": "Run `graphene risk baseline` on the Graphene host …"}`.
  `changes_since_previous` is absent with it. First call on a fresh session took 10.5 s (G-1 test
  expects < 3 s); later calls 0.4–0.8 s.
- **Expected:** a current score (0.47 / high / "88th percentile"; 0.345 / moderate).
  Graphene's own fix: `graphene risk baseline` on the Graphene host. The app can't and won't run it.
- **Also seen:** one warm `impact ZS4_IOT_DATA` took 5,712 ms (usually ~700 ms).

## G-7 · MCP handshake still reports 1.31.0 with v12 results — RESOLVED

- **Resolved in demo-ready-v13 (installed 11:13):** the handshake reports `1.32.0`.

- **Found:** 2026-11-16. Results carry new v12 fields (`comparable`, `changes_from_previous`, …), but
  `serverInfo.version` is `1.31.0`, the same as v11. Partners can't tell which build answered.
  Expected: the v12 version.
