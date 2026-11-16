# SAP Assistant · sample partner app

A **sample partner app** built by the Graphene team to show how a partner's own AI application plugs
into Graphene. It is not Graphene and not a real partner. It takes a question, lets Claude call
Graphene's MCP tools, and explains the answer with the evidence shown. Every SAP fact comes from Graphene.

## Run

```bash
make setup        # uv sync (Python 3.12)
make test         # mocked Claude, real Graphene MCP — no API spend
make run          # http://127.0.0.1:8080 (localhost only)
make latency      # Graphene tool latency on one warm MCP session
make eval-estimate  # token count + cost estimate for the golden questions (free)
make eval         # golden questions against the real Claude API (costs money)
```

From the operator laptop: `ssh -p 2222 -L 8080:127.0.0.1:8080 grpadm@127.0.0.1`, then http://localhost:8080.

`.env` (chmod 600, gitignored) holds `ANTHROPIC_API_KEY`; see `.env.example`.

## How it works

- `app/mcp_client.py` — spawns `graphene-mcp` **once** at app start (stdio), keeps that session for all
  questions, watches the process and respawns only if it dies (one retry for a call cut off by that).
- `app/agent.py` — per question: a fresh conversation with `claude-sonnet-5-5` (adaptive thinking,
  effort medium), Graphene's own tool schemas, max 8 tool calls, every call timed; the final answer
  is JSON (`answer`, `claims[].evidence_ids`) via structured output.
- `app/contract.py` — adds `tool_trace`, and `coverage` / `freshness` / `limits` copied verbatim from
  Graphene's results.
- `app/grounding.py` — flags any object name, pseudonym, number or evidence id in the answer that is
  not in this question's tool results (yellow in the UI, never hidden).
- `app/static/index.html` — one self-contained page (no CDNs), dark/light, streamed progress.

Logs (`logs/app.jsonl`) hold questions, tool calls, timings and token usage — never the key.
Problems: app issues are fixed here (`TEST_LOG.md`); wrong or missing facts go to `GRAPHENE_ISSUES.md`.
