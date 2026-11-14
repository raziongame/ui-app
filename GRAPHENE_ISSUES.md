# Graphene issues found by the sample partner app

Facts (and fact-level gaps) that are Graphene's to fix. The app never works around them.
Each entry: question, what Graphene returned, what's expected. Graphene `1.30.0` over MCP stdio.

---

## G-1 · First `assess_transport` on a new MCP session takes 43–59 s (warm: ~0.4 s)

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

## G-2 · No earlier risk score to answer "riskier than before"

- **Found:** 2026-11-14, golden question 3.
- **Question:** "Why is ZUZK900006 riskier than before?"
- **What Graphene returned:** `score 0.345`, `band moderate`, reasons (incl. "reaches periodic job
  JOB:JOB-5192e9d4d2ab in 1 hop (runs daily, 12 runs in window)" via `PROG:ZGR_PRICE_SYNC`), and
  `comparison_version.since_last`: "nothing changed since the last assessment (2026-11-12 14:44)".
  There is no previous score, band or reason list to compare against.
- **Expected:** to answer "riskier than before", `assess_transport` should return the previous
  assessment's score/band (and what changed), or say explicitly that it has none. Today the app can
  only say "Graphene did not return an earlier score".

## G-3 · Percentile is a fraction (0.8833); the golden expectation says "88th percentile"

- **Found:** 2026-11-14, golden question 2.
- **Question:** "Why is ZUZK900008 high risk?"
- **What Graphene returned:** `percentile: 0.8833`.
- **Expected (CLAUDE.md):** "88th percentile". The app may not convert or round numbers, so it shows
  0.8833. If "88th percentile" is the wording users should see, Graphene should return it (e.g. a
  `percentile_text`), or the golden expectation should change to 0.8833. Operator to decide.
