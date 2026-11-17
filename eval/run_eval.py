"""Golden questions against the REAL Claude API (and real Graphene MCP).

  python eval/run_eval.py --estimate [--only ids]   # token counts via count_tokens (free) -> cost estimate
  python eval/run_eval.py --only id1 id2             # fixing: only the affected questions, cap $1 per run
  python eval/run_eval.py --full                     # every golden question: only before a tag

Cost rule (operator 2026-11-17): the full eval only before a tag; while fixing, only the affected
questions, and a run stops before it would pass --max-cost (default $1).

The key is read from .env by app.config and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import anthropic
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent, contract, config, present, wording  # noqa: E402
from app.mcp_client import GrapheneMCP  # noqa: E402

# Claude Sonnet 5.5, $ per million tokens (Anthropic first-party pricing).
PRICE = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}
# Output assumptions for the estimate (adaptive thinking at medium effort is billed as output).
OUT_PER_TOOL_TURN = 500
OUT_FINAL = 1500


RESULTS_DIR = ROOT / "eval" / "results"
ONLY: set[str] = set()
MAX_COST = [1.00]  # $ per run (operator 2026-11-17); make eval (full, before a tag) passes its own cap
TARGET_S = 10.0  # operator 2026-11-16: under 10 s per answer (a case may set expect.max_seconds)


def question_of(case: dict) -> str:
    """The message as the user sends it (Q10: the pasted note excerpt, used exactly, plus the question)."""
    if "question_file" in case:
        return ((ROOT / case["question_file"]).read_text() + case.get("question_suffix", "")).strip()
    return case["question"]


def cases() -> list[dict]:
    all_cases = yaml.safe_load((ROOT / "tests" / "golden_questions.yaml").read_text())
    out = [c for c in all_cases if not c.get("test_only") and (not ONLY or c["id"] in ONLY)]
    for c in out:
        c["question"] = question_of(c)
    return out


def dollars(u: dict) -> float:
    return (u.get("input_tokens", 0) * PRICE["input"] + u.get("output_tokens", 0) * PRICE["output"]
            + u.get("cache_creation_input_tokens", 0) * PRICE["cache_write"]
            + u.get("cache_read_input_tokens", 0) * PRICE["cache_read"]) / 1e6


def new_client() -> anthropic.AsyncAnthropic:
    key = config.load_api_key()
    if not key:
        sys.exit("No ANTHROPIC_API_KEY in .env")
    return anthropic.AsyncAnthropic(api_key=key, timeout=config.CLAUDE_REQUEST_TIMEOUT_S, max_retries=2)


async def estimate(g: GrapheneMCP, client: anthropic.AsyncAnthropic) -> None:
    system = agent.system_prompt(g.instructions)
    tools = agent._tools_for_claude(g)
    tot_nocache = tot_cache = 0.0
    print(f"{'question':45} {'turns':>5} {'input tok':>10} {'no cache $':>11} {'w/ cache $':>11}")
    for c in cases():
        msgs: list[dict] = [{"role": "user", "content": c["question"]}]
        turn_inputs = []
        for i, call in enumerate(c["mock"]["calls"]):
            n = await client.messages.count_tokens(model=config.MODEL, system=system, tools=tools, messages=msgs)
            turn_inputs.append(n.input_tokens)
            r = await g.call(call["tool"], call.get("args", {}))
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"toolu_{i}", "name": call["tool"], "input": call.get("args", {})}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}",
                                                      "content": json.dumps(r.structured, separators=(",", ":"))}]})
        n = await client.messages.count_tokens(model=config.MODEL, system=system, tools=tools, messages=msgs)
        turn_inputs.append(n.input_tokens)
        turns = len(turn_inputs)
        out_tok = OUT_PER_TOOL_TURN * (turns - 1) + OUT_FINAL
        nocache = (sum(turn_inputs) * PRICE["input"] + out_tok * PRICE["output"]) / 1e6
        # with automatic caching: each turn writes its new tokens, and reads the previous prefix
        cached = turn_inputs[0] * PRICE["cache_write"]
        for prev, cur in zip(turn_inputs, turn_inputs[1:]):
            cached += prev * PRICE["cache_read"] + (cur - prev) * PRICE["cache_write"]
        cached = (cached + out_tok * PRICE["output"]) / 1e6
        tot_nocache += nocache
        tot_cache += cached
        print(f"{c['question'].splitlines()[-1][:45]:45} {turns:>5} {sum(turn_inputs):>10} {nocache:>11.4f} {cached:>11.4f}")
    print(f"{f'TOTAL ({len(cases())} questions)':45} {'':>5} {'':>10} {tot_nocache:>11.4f} {tot_cache:>11.4f}")
    print(f"Assumes the mock's tool calls and ~{OUT_PER_TOOL_TURN} output tokens per tool turn, "
          f"~{OUT_FINAL} for the final answer. If Claude makes twice the calls, budget about 2x.")


async def run(g: GrapheneMCP, client: anthropic.AsyncAnthropic) -> int:
    results, failures, total_cost = [], 0, 0.0
    planned, worst = cases(), 0.0
    for i, c in enumerate(planned):
        # stop before the next question could take the run past the cap (worst cost so far, else $0.10)
        if total_cost + max(worst, 0.10) > MAX_COST[0]:
            left = planned[i:]
            print(f"STOPPED: cost cap ${MAX_COST[0]:.2f} (spent ${total_cost:.4f}); not run: "
                  + ", ".join(x["id"] for x in left))
            failures += len(left)
            break
        t0 = time.time()
        try:
            out = await agent.answer_with_timeout(c["question"], g, client)
        except agent.AnswerError as e:
            cost = dollars(e.usage)
            total_cost += cost
            worst = max(worst, cost)
            results.append({"id": c["id"], "question": c["question"], "error": str(e),
                            "tool_trace": e.trace, "usage": e.usage, "cost_usd": round(cost, 4)})
            failures += 1
            print(f"FAIL {c['id']}: {e}  ${cost:.4f}  {len(e.trace)} tool calls: "
                  + ", ".join(f"{t['tool']}{json.dumps(t['arguments'])}" for t in e.trace))
            continue
        exp, problems, notes = c["expect"], [], []
        shown = present.finalize(out)  # what the user sees (silent grounding applied)
        outcome = out["presentation"]["outcome"]
        if outcome in ("held back", "fallback"):
            problems.append(f"{outcome}: {out['presentation']['unbacked']}")
        elif outcome == "rewritten":
            notes.append(f"rewritten (first draft: {[f['token'] for f in out['first_draft_flags']]})")
        for p in wording.problems(shown):
            problems.append(p)
        called = {t["tool"] for t in out["tool_trace"]}
        for need in exp.get("tools", []):
            opts = need if isinstance(need, list) else [need]
            if not called & set(opts):
                problems.append(f"tool not called: {opts}")
        text = shown.lower()
        for item in exp.get("answer_contains", []):
            opts = item if isinstance(item, list) else [item]
            if not any(str(o).lower() in text for o in opts):
                problems.append(f"answer lacks {opts}")
        for bad in exp.get("answer_not_contains", []):
            if str(bad).lower() in text:
                problems.append(f"answer contains {bad!r}")
        facts_text = json.dumps(out["system_facts"], ensure_ascii=False)
        for item in exp.get("facts_contain", []):
            if str(item) not in facts_text:
                problems.append(f"system facts lack {item!r}")
        for bad in exp.get("prose_not_contains", []):
            if str(bad).lower() in out["answer"].lower():
                problems.append(f"answer text contains {bad!r} (belongs in the system facts only)")
        prose = out["answer"]
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", prose.strip()) if x]
        for item in exp.get("first_sentence_contains", []):
            opts = item if isinstance(item, list) else [item]
            if not sentences or not any(str(o).lower() in sentences[0].lower() for o in opts):
                problems.append(f"first sentence lacks {opts!r}")
        if exp.get("max_sentences") and len(sentences) > exp["max_sentences"]:
            problems.append(f"answer has {len(sentences)} sentences > {exp['max_sentences']}")
        if exp.get("no_change_only_at_end"):
            for sent in sentences[:-1]:
                if re.search(r"nothing (has )?changed|no changes?\b|unchanged|same score", sent, re.I):
                    problems.append(f"'no change' wording before the end: {sent[:60]!r}")
        hist = out.get("history") or {}
        if "history_steps" in exp:
            got = [{k: st[k] for k in ("at", "from", "to", "older_engine", "causes")} for st in hist.get("steps", [])]
            if got != exp["history_steps"]:
                problems.append(f"history table differs from Graphene's steps: {got}")
            if hist.get("nothing_changed_since_last") != exp.get("nothing_changed_since_last"):
                problems.append("history table: 'nothing changed' line missing or wrong")
        elif hist.get("steps"):
            problems.append(f"history table shown for {hist.get('transport')} (not a history question)")
        if exp.get("max_words") and len(prose.split()) > exp["max_words"]:
            problems.append(f"answer has {len(prose.split())} words > {exp['max_words']}")
        for rx in exp.get("answer_not_matches", []):
            if re.search(rx, prose, re.I):
                problems.append(f"answer matches {rx!r}")
        secs = time.time() - t0
        if secs > exp.get("max_seconds", TARGET_S):
            problems.append(f"slow: {secs:.1f} s > {exp.get('max_seconds', TARGET_S)} s")
        if [t["tool"] for t in out["tool_trace"]].count("status") > 1:
            problems.append("status called more than once")
        cost = dollars(out["usage"])
        total_cost += cost
        worst = max(worst, cost)
        failures += bool(problems)
        results.append({"id": c["id"], "question": c["question"], "ok": not problems, "problems": problems,
                        "notes": notes, "shown": shown, "cost_usd": round(cost, 4), "seconds": round(secs, 1),
                        **{k: v for k, v in out.items() if k != "new_results"}})
        print(f"{'PASS' if not problems else 'FAIL'} {c['id']}  ${cost:.4f}  {secs:.1f}s"
              + f"  {out['usage']['requests']} requests, {len(out['tool_trace'])} calls"
              + "".join(f"  [{n}]" for n in notes)
              + ("".join(f"\n     - {p}" for p in problems)))
    outdir = RESULTS_DIR
    outdir.mkdir(exist_ok=True)
    path = outdir / f"eval-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str))
    print(f"\n{'question':45} {'total':>7} {'limit':>6} {'cost':>8}  result")
    for r, c in zip(results, planned):
        tot = f"{r['seconds']:.1f} s" if "seconds" in r else "-"
        lim = f"{c['expect'].get('max_seconds', TARGET_S):g} s"
        print(f"{c['question'].splitlines()[-1][:45]:45} {tot:>7} {lim:>6} {'$%.4f' % r['cost_usd']:>8}  "
              + ("PASS" if r.get("ok") else "FAIL"))
    passed = sum(1 for r in results if r.get("ok"))
    print(f"\n{passed}/{len(planned)} passed · actual cost ${total_cost:.4f} · {path.relative_to(ROOT)}")
    return 1 if failures else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--only", nargs="*", default=[], help="golden case ids to run")
    ap.add_argument("--full", action="store_true", help="every golden question (only before a tag)")
    ap.add_argument("--max-cost", type=float, default=MAX_COST[0], help="$ cap for this run (default 1.00)")
    args = ap.parse_args()
    if not args.estimate and not args.full and not args.only:
        sys.exit("Name the affected questions (--only id ...), or --full before a tag (operator cost rule).")
    MAX_COST[0] = args.max_cost
    ONLY.update(args.only)
    g = GrapheneMCP(config.GRAPHENE_MCP_COMMAND, call_timeout_s=config.TOOL_TIMEOUT_S)
    await g.start()
    client = new_client()
    try:
        if args.estimate:
            await estimate(g, client)
            return 0
        return await run(g, client)
    finally:
        await g.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
