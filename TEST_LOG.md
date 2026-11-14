# Test log

One row per reported problem. Owner = **app** (fixed here) or **Graphene** (written up in GRAPHENE_ISSUES.md).

| Date | Question | Expected | Got | Verdict | Owner | Fix commit |
|---|---|---|---|---|---|---|
| 2026-11-14 | Why is ZUZK900008 high risk? | "88th percentile" (CLAUDE.md) | Graphene returns `percentile: 0.8833`; app shows 0.8833 (no conversion allowed) | Golden case expects 0.8833 and rejects "88th"; wording question raised | Graphene (G-3) | — |
| 2026-11-14 | Why is ZUZK900006 riskier than before? | 0.345, daily job via ZGR_PRICE_SYNC (12 runs) | Those facts, but no earlier score to compare | App says Graphene returned no earlier score | Graphene (G-2) | — |
| 2026-11-14 | (any, first risk question after start) | answer in seconds | first `assess_transport` 43–59 s; warm 0.4 s | One MCP session kept for all questions | Graphene (G-1) | — |
| 2026-11-14 | (any, first risk question after start) | first assess < 3 s | Graphene demo-ready-v10: 544 ms on a fresh session | G-1 resolved; latency regression test added | Graphene (G-1) | see git log |
| 2026-11-14 | Why is ZUZK900008 high risk? | "88th percentile" | Graphene returns `percentile_label: "88th percentile"` | G-3 resolved; golden expects the label; prompt uses Graphene labels | Graphene (G-3) + app prompt | see git log |
| 2026-11-14 | Why is ZUZK900006 riskier than before? | why it got riskier | Graphene: previous 0.345 / moderate on 2026-11-12, no changes → not riskier | G-2 resolved; golden expects the app to correct the premise; prompt rule added | Graphene (G-2) + app prompt | see git log |
