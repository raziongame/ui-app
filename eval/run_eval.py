"""Golden questions against the REAL Claude API (and real Graphene MCP).

  python eval/run_eval.py --estimate   # token counts via count_tokens (free) -> cost estimate, no answers
  python eval/run_eval.py              # runs the golden questions (costs money), checks expectations

The key is read from .env by app.config and never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import anthropic
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent, config  # noqa: E402
from app.mcp_client import GrapheneMCP  # noqa: E402

# Claude Sonnet 5.5, $ per million tokens (Anthropic first-party pricing).
PRICE = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}
# Output assumptions for the estimate (adaptive thinking at medium effort is billed as output).
OUT_PER_TOOL_TURN = 500
OUT_FINAL = 1500


def cases() -> list[dict]:
    return [c for c in yaml.safe_load((ROOT / "tests" / "golden_questions.yaml").read_text()) if not c.get("test_only")]


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
        try:
            out = await agent.answer_with_timeout(c["question"], g, client)
        except agent.AnswerError as e:
            results.append({"id": c["id"], "question": c["question"], "error": str(e)})
            failures += 1
            print(f"FAIL {c['id']}: {e}")
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
        for bad in exp.get("answer_not_contains", []):
            if str(bad).lower() in text:
                problems.append(f"answer contains {bad!r}")
        if len(out["grounding"]["flags"]) > exp.get("grounding_flags_max", 0):
            problems.append(f"grounding flags: {[f['token'] for f in out['grounding']['flags']]}")
        cost = dollars(out["usage"])
        total_cost += cost
        failures += bool(problems)
        results.append({"id": c["id"], "question": c["question"], "ok": not problems, "problems": problems,
                        "cost_usd": round(cost, 4), "seconds": round(time.time() - t0, 1), **out})
        print(f"{'PASS' if not problems else 'FAIL'} {c['id']}  ${cost:.4f}  {time.time() - t0:.1f}s"
              + ("".join(f"\n     - {p}" for p in problems)))
    outdir = ROOT / "eval" / "results"
    outdir.mkdir(exist_ok=True)
    path = outdir / f"eval-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"\n{len(results) - failures}/{len(results)} passed · actual cost ${total_cost:.4f} · {path.relative_to(ROOT)}")
    return 1 if failures else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    args = ap.parse_args()
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
