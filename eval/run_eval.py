"""Golden questions against the REAL Claude API (and real Graphene MCP).

  python eval/run_eval.py --estimate   # token counts via count_tokens (free) -> cost estimate, no answers
  python eval/run_eval.py              # runs the golden questions (costs money), checks expectations

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

from app import agent, contract, config  # noqa: E402
from app.mcp_client import GrapheneMCP  # noqa: E402

# Claude Sonnet 5.5, $ per million tokens (Anthropic first-party pricing).
PRICE = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}
# Output assumptions for the estimate (adaptive thinking at medium effort is billed as output).
OUT_PER_TOOL_TURN = 500
OUT_FINAL = 1500


ONLY: set[str] = set()
TARGET_S = 10.0  # operator 2026-11-16: under 10 s per answer (a case may set expect.max_seconds)


def cases() -> list[dict]:
    all_cases = yaml.safe_load((ROOT / "tests" / "golden_questions.yaml").read_text())
    return [c for c in all_cases if not c.get("test_only") and (not ONLY or c["id"] in ONLY)]


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
    system = agent.SYSTEM_PROMPT.format(max_calls=config.MAX_TOOL_CALLS, instructions=g.instructions.strip())
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
        print(f"{c['question'][:45]:45} {turns:>5} {sum(turn_inputs):>10} {nocache:>11.4f} {cached:>11.4f}")
    print(f"{'TOTAL (5 questions, 1 run)':45} {'':>5} {'':>10} {tot_nocache:>11.4f} {tot_cache:>11.4f}")
    print(f"Assumes the mock's tool calls and ~{OUT_PER_TOOL_TURN} output tokens per tool turn, "
          f"~{OUT_FINAL} for the final answer. If Claude makes twice the calls, budget about 2x.")


async def run(g: GrapheneMCP, client: anthropic.AsyncAnthropic) -> int:
    results, failures, total_cost = [], 0, 0.0
    for c in cases():
        t0 = time.time()
        first_text: list[float] = []  # seconds until the first answer text is visible in the UI

        async def emit(ev: dict, _t0: float = t0, _ft: list = first_text) -> None:
            if ev.get("type") == "answer_delta" and not _ft:
                _ft.append(time.time() - _t0)

        try:
            out = await agent.answer_with_timeout(c["question"], g, client, emit)
        except agent.AnswerError as e:
            cost = dollars(e.usage)
            total_cost += cost
            results.append({"id": c["id"], "question": c["question"], "error": str(e),
                            "tool_trace": e.trace, "usage": e.usage, "cost_usd": round(cost, 4)})
            failures += 1
            print(f"FAIL {c['id']}: {e}  ${cost:.4f}  {len(e.trace)} tool calls: "
                  + ", ".join(f"{t['tool']}{json.dumps(t['arguments'])}" for t in e.trace))
            continue
        exp, problems = c["expect"], []
        called = {t["tool"] for t in out["tool_trace"]}
        for need in exp.get("tools", []):
            opts = need if isinstance(need, list) else [need]
            if not called & set(opts):
                problems.append(f"tool not called: {opts}")
        text = (out["answer"] + "\n" + "\n".join(cl["text"] for cl in out["claims"])).lower()
        for item in exp.get("answer_contains", []):
            opts = item if isinstance(item, list) else [item]
            if not any(str(o).lower() in text for o in opts):
                problems.append(f"answer lacks {opts}")
        if exp.get("lead_with"):
            lead, rest = exp["lead_with"].lower(), out["answer"].lower()
            for later in ("no changes", "nothing changed", "same score"):
                if later in rest and lead in rest and rest.index(later) < rest.index(lead):
                    problems.append(f"{later!r} comes before {exp['lead_with']!r}")
        for bad in exp.get("answer_not_contains", []):
            if str(bad).lower() in text:
                problems.append(f"answer contains {bad!r}")
        prose, claims_text = out["answer"].lower(), "\n".join(cl["text"] for cl in out["claims"]).lower()
        dev = contract.developer_words(out["answer"])
        if dev:  # operator 2026-11-16: plain language; field names stay in the evidence panel
            problems.append(f"field names / JSON words in the answer: {dev}")
        for bad in exp.get("prose_not_contains", []):
            if str(bad).lower() in prose:
                problems.append(f"answer text contains {bad!r} (belongs in the evidence panel)")
        for item in exp.get("claims_contain", []):
            if str(item).lower() not in claims_text:
                problems.append(f"claims lack {item!r}")
        if exp.get("max_words") and len(out["answer"].split()) > exp["max_words"]:
            problems.append(f"answer has {len(out['answer'].split())} words > {exp['max_words']}")
        for rx in exp.get("answer_not_matches", []):
            if re.search(rx, text, re.I):
                problems.append(f"answer matches {rx!r}")
        secs = time.time() - t0
        if secs > exp.get("max_seconds", TARGET_S):
            problems.append(f"slow: {secs:.1f} s > {exp.get('max_seconds', TARGET_S)} s")
        if [t["tool"] for t in out["tool_trace"]].count("status") > 1:
            problems.append("status called more than once")
        if len(out["grounding"]["flags"]) > exp.get("grounding_flags_max", 0):
            problems.append(f"grounding flags: {[f['token'] for f in out['grounding']['flags']]}")
        cost = dollars(out["usage"])
        total_cost += cost
        failures += bool(problems)
        results.append({"id": c["id"], "question": c["question"], "ok": not problems, "problems": problems,
                        "cost_usd": round(cost, 4), "seconds": round(time.time() - t0, 1),
                        "first_text_s": round(first_text[0], 1) if first_text else None, **out})
        print(f"{'PASS' if not problems else 'FAIL'} {c['id']}  ${cost:.4f}  {time.time() - t0:.1f}s"
              + f"  {out['usage']['requests']} requests, {len(out['tool_trace'])} calls"
              + (f", {out['usage']['repeat_calls_skipped']} repeat(s) skipped" if out["usage"].get("repeat_calls_skipped") else "")
              + ("".join(f"\n     - {p}" for p in problems)))
    outdir = ROOT / "eval" / "results"
    outdir.mkdir(exist_ok=True)
    path = outdir / f"eval-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"\n{'question':45} {'first text':>10} {'total':>7} {'limit':>6} {'cost':>8}  result")
    for r, c in zip(results, cases()):
        ft = f"{r['first_text_s']:.1f} s" if r.get("first_text_s") is not None else "-"
        tot = f"{r['seconds']:.1f} s" if "seconds" in r else "-"
        lim = f"{c['expect'].get('max_seconds', TARGET_S):g} s"
        print(f"{r['question'][:45]:45} {ft:>10} {tot:>7} {lim:>6} {'$%.4f' % r['cost_usd']:>8}  "
              + ("PASS" if r.get("ok") else "FAIL"))
    print(f"\n{len(results) - failures}/{len(results)} passed · actual cost ${total_cost:.4f} · {path.relative_to(ROOT)}")
    return 1 if failures else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--only", nargs="*", default=[], help="golden case ids to run")
    args = ap.parse_args()
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
